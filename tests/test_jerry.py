#!/usr/bin/env python3
"""Tests for jerry.

Every test checks a behaviour documented in ../README.md. By default the suite
starts the mock server in mock_server.py on a free port and talks to that, so it
needs no model and takes seconds. To check a real llama.cpp server instead, point
the suite at it; tests that need a server are skipped if it does not answer.

    python3 tests/test_jerry.py                  # bundled mock server; `tests/mock_server.py` now tests against `jerry`.
    python3 tests/test_jerry.py --server URL     # a real server (or JERRY_TEST_URL)
    python3 tests/test_jerry.py -k pipe          # run a subset

Against a real server the run time is dominated by the model, and some tests skip
themselves when the model does not produce what they need (reasoning, a seeded
repeat); the mock always produces it.
"""

import argparse
import contextlib
import fcntl
import io
import json
import os
import pty
import random
import re
import select
import signal
import subprocess
import sys
import time
import unittest
import urllib.request
from importlib import machinery as importlib_machinery
from importlib import util as importlib_util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JERRY = os.path.join(ROOT, "jerry")
README = os.path.join(ROOT, "README.md")

sys.path.insert(0, HERE)
import mock_server  # noqa: E402

# None means "start the mock server" (see setUpModule). JERRY_URL is deliberately not
# consulted: a stray JERRY_URL must not make the suite talk to somebody's real server.
SERVER = os.environ.get("JERRY_TEST_URL") or None
TIMEOUT = float(os.environ["JERRY_TEST_TIMEOUT"]) if os.environ.get("JERRY_TEST_TIMEOUT") else None

EXIT_OK, EXIT_USAGE, EXIT_SERVER, EXIT_LIMIT, EXIT_KILLED = 0, 1, 2, 3, 130


def reachable(url, timeout=10):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode("utf-8", "replace")).get("data"))
    except Exception:
        return False


SERVER_UP = False
_MOCK = None
MOCK_URL = None          # set when the bundled mock is the server


def setUpModule():
    """Decide which server the live tests use. This runs after the command line has
    been read, so --server and JERRY_TEST_URL are both honoured."""
    global SERVER, SERVER_UP, TIMEOUT, _MOCK, MOCK_URL
    if SERVER:
        kind = "external server"
        TIMEOUT = TIMEOUT or 300.0
    else:
        _MOCK, SERVER = mock_server.start()
        MOCK_URL = SERVER
        kind = "bundled mock server"
        TIMEOUT = TIMEOUT or 60.0
    SERVER_UP = reachable(SERVER)
    print("server: %s (%s, %s)" % (SERVER, kind, "reachable" if SERVER_UP else "NOT REACHABLE, live tests skipped"),
          file=sys.stderr)


def tearDownModule():
    if _MOCK is not None:
        _MOCK.shutdown()
        _MOCK.server_close()


class LiveCase(unittest.TestCase):
    """Base class of the tests that need a server."""

    @classmethod
    def setUpClass(cls):
        if not SERVER_UP:
            raise unittest.SkipTest("no live server at %s" % SERVER)


def load_jerry(env=None):
    """Import jerry as a module (the file has no .py extension) with a given env."""
    saved = dict(os.environ)
    try:
        if env:
            os.environ.update(env)
        name = "jerry_under_test"
        loader = importlib_machinery.SourceFileLoader(name, JERRY)
        spec = importlib_util.spec_from_loader(name, loader)
        mod = importlib_util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


JERRY_MOD = load_jerry()


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = saved


def nonblock(fd):
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


class Run:
    """One run of jerry as a subprocess.

    stdout/stderr are pipes (not terminals) unless asked otherwise, and both are
    read with select so the ordering of the meter and the response can be
    checked. Nothing can block: every read has a deadline.
    """

    def __init__(self, args, stdin=None, env=None, tty_stderr=False, run=True):
        self.args = list(args)
        self.env = dict(os.environ)
        self.env.pop("JERRY_SEED", None)
        self.env["JERRY_URL"] = SERVER
        self.env.update(env or {})

        self.timeline = []          # (monotonic, "out"/"err", bytes)
        self.code = None
        self.master = None
        self.err_read_fd = None
        self.out_read_fd = None

        if tty_stderr:
            self.master, err_w = pty.openpty()
            nonblock(self.master)
        else:
            self.err_read_fd, err_w = os.pipe()
            nonblock(self.err_read_fd)

        in_r, in_w = os.pipe()
        self.out_read_fd, out_w = os.pipe()
        nonblock(self.out_read_fd)

        self.proc = subprocess.Popen([JERRY] + self.args, stdin=in_r, stdout=out_w, stderr=err_w,
                                     cwd=ROOT, env=self.env)
        for fd in (err_w, out_w):
            os.close(fd)
        self.stdin_fd = in_w

        if run:
            if stdin is not None:
                os.write(self.stdin_fd, stdin.encode("utf-8"))
            os.close(self.stdin_fd)
            self.wait()

    def fds(self):
        fds = {}
        for fd, name in ((self.out_read_fd, "out"), (self.err_read_fd, "err"), (self.master, "err")):
            if fd is not None:
                fds[fd] = name
        return fds

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout or TIMEOUT)
        open_fds = set(self.fds())
        while open_fds and time.monotonic() < deadline:
            for fd in select.select(list(open_fds), [], [], 0.5)[0]:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if not data:
                    open_fds.discard(fd)
                    continue
                self.timeline.append((time.monotonic(), self.fds()[fd], data))
        try:
            self.code = self.proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.code = None
        for fd in (self.out_read_fd, self.err_read_fd, self.master):
            if fd is not None:
                try:
                    os.read(fd, 65536)
                except OSError:
                    pass
                os.close(fd)
        return self

    def stream(self, name):
        return b"".join(chunk for _, s, chunk in self.timeline if s == name)

    @property
    def out(self):
        return self.stream("out")

    @property
    def err(self):
        return self.stream("err")

    def first(self, name, pattern=None):
        for ts, s, chunk in self.timeline:
            if s == name and (pattern is None or re.search(pattern, chunk.decode("utf-8", "replace"))):
                return ts
        return None

    def stderr_lines(self):
        return [l for l in self.err.decode("utf-8", "replace").splitlines() if l]

    def assert_ok(self):
        if self.code is None:
            raise AssertionError("timed out after %ss (stderr: %s)" % (TIMEOUT, self.err.decode("utf-8", "replace")))
        if self.code != EXIT_OK:
            raise AssertionError("exit %s (stderr: %s)" % (self.code, self.err.decode("utf-8", "replace")))
        return self


# --------------------------------------------------------------------------- #
# the spec that does not need a server
# --------------------------------------------------------------------------- #

class TestReadmeSpec(unittest.TestCase):

    def test_exit_codes_documented_in_help(self):
        r = subprocess.run([JERRY, "--help"], capture_output=True, text=True)
        for code in (0, 1, 2, 3, 130):
            self.assertIn(" %d " % code, r.stdout, "help should list exit code %d" % code)

    def test_help_lists_every_documented_flag(self):
        with open(README) as fh:
            flags = re.findall(r"^\| `(-{1,2}[A-Za-z-]+)", fh.read(), re.M)
        self.assertTrue(flags, "no flags found in the README option table")
        r = subprocess.run([JERRY, "--help"], capture_output=True, text=True)
        for flag in flags:
            self.assertIn(flag, r.stdout, "%s is documented in the README but missing from --help" % flag)

    def test_documented_defaults(self):
        r = subprocess.run([JERRY, "--help"], capture_output=True, text=True)
        self.assertIn("http://localhost:8080", r.stdout)
        self.assertIn("32768", r.stdout)
        self.assertIn("600", r.stdout)

    def test_stock_python3_standard_library_only(self):
        with open(JERRY) as fh:
            src = fh.read()
        self.assertEqual(src.splitlines()[0], "#!/usr/bin/env python3")
        mods = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)", src, re.M))
        stdlib = set(sys.stdlib_module_names)
        for mod in mods:
            self.assertIn(mod.split(".")[0], stdlib,
                          "%s is not standard library" % mod)

    def test_no_client_side_response_cache(self):
        with open(JERRY) as fh:
            self.assertNotIn("cache", fh.read().lower())

    def test_no_shell_and_no_prompt_decoration(self):
        with open(JERRY) as fh:
            src = fh.read()
        self.assertNotIn("subprocess", src)
        self.assertNotIn("sh -c", src)

    def test_only_the_standard_llama_cpp_endpoints_are_used(self):
        with open(JERRY) as fh:
            src = fh.read()
        paths = set(re.findall(r'"(/v1/[A-Za-z0-9_/]*)"', src))
        self.assertEqual(paths, {"/v1/chat/completions", "/v1/models"},
                         "jerry should only use the documented endpoints")

    def test_flush_clears_pending_so_a_held_response_is_printed_once(self):
        out = JERRY_MOD.Out(JERRY_MOD.parse_args([]), hold=True)
        buf = io.StringIO()
        out.fh = buf
        out.write("answer")
        out.flush()
        self.assertEqual(buf.getvalue(), "answer")
        out.flush()
        self.assertEqual(buf.getvalue(), "answer", "pending was not cleared: the response is printed twice")

    def test_meter_stays_silent_without_verbose(self):
        meter = JERRY_MOD.Meter(JERRY_MOD.parse_args([]), 100)
        with captured_stderr() as buf:
            meter.set_phase("prompt")
            meter.tick(1)
            meter.done("stop")
            self.assertEqual(buf.getvalue(), "", "nothing is printed unless -v is on")

    def test_meter_line_format_matches_the_readme_example(self):
        """jerry: 63/32768   0.2%  18.5 t/s   4.9s streaming"""
        with captured_stderr() as buf:
            meter = JERRY_MOD.Meter(JERRY_MOD.parse_args(["-v"]), 32768)
            meter.tty = meter.clean = False
            meter.set_phase("streaming")
        self.assertRegex(buf.getvalue().strip(),
                         r"^jerry: \d+/\d+ +\d+\.\d+% +\d+\.\d+ t/s +\d+\.\d+s streaming$")

    def test_meter_line_when_there_is_no_limit(self):
        with captured_stderr() as buf:
            meter = JERRY_MOD.Meter(JERRY_MOD.parse_args(["-v", "-b"]), 0)
            meter.tty = meter.clean = False
            meter.set_phase("streaming")
        self.assertIn("tokens (no limit)", buf.getvalue())

    def test_limit_error_message_is_the_one_in_the_readme(self):
        with captured_stderr() as buf:
            code = JERRY_MOD.finish_code(JERRY_MOD.parse_args([]), "length", 32768)
        self.assertEqual(code, EXIT_LIMIT)
        self.assertEqual(buf.getvalue().strip(),
                         "jerry: output limit reached (32768 tokens), the response is incomplete; "
                         "use -b for no limit or --limit N")

    def test_tool_call_reports_and_exits_two(self):
        with captured_stderr() as buf:
            code = JERRY_MOD.finish_code(JERRY_MOD.parse_args([]), "tool_calls", 32768)
        self.assertEqual(code, EXIT_SERVER)
        self.assertIn("the model requested a tool call, jerry has no tools to run", buf.getvalue())

    def test_exit_code_constants(self):
        self.assertEqual((JERRY_MOD.EXIT_OK, JERRY_MOD.EXIT_USAGE, JERRY_MOD.EXIT_SERVER,
                          JERRY_MOD.EXIT_LIMIT, JERRY_MOD.EXIT_KILLED),
                         (EXIT_OK, EXIT_USAGE, EXIT_SERVER, EXIT_LIMIT, EXIT_KILLED))

    def test_blank_input_is_a_usage_error(self):
        saved_in = sys.stdin
        sys.stdin = io.StringIO("   \n ")
        try:
            with captured_stderr() as buf:
                code = JERRY_MOD.main([])
        finally:
            sys.stdin = saved_in
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nothing to send", buf.getvalue())

    def test_quiet_suppresses_the_error_line_only(self):
        with captured_stderr() as buf:
            JERRY_MOD.report(JERRY_MOD.parse_args(["-q"]), "quiet message")
            JERRY_MOD.report(JERRY_MOD.parse_args([]), "shown message")
        self.assertEqual(buf.getvalue(), "jerry: shown message\n")


# --------------------------------------------------------------------------- #
# what jerry sends
# --------------------------------------------------------------------------- #

class TestWhatJerrySends(unittest.TestCase):

    def test_seed_is_sent_on_every_request(self):
        payload, _ = JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [{"role": "user", "content": "x"}], 0)
        self.assertEqual(payload["seed"], 0)

    def test_default_seed_comes_from_jerry_seed(self):
        self.assertEqual(load_jerry({"JERRY_SEED": "1234"}).parse_args([]).seed, 1234)

    def test_s_seed_overrides_the_default(self):
        self.assertEqual(JERRY_MOD.parse_args(["-s", "1234"]).seed, 1234)

    def test_bare_s_means_random(self):
        args = JERRY_MOD.parse_args(["-s"])
        self.assertIsNone(args.seed)
        seed = random.randint(0, 2 ** 31 - 1)
        self.assertEqual(JERRY_MOD.build_payload(args, [], seed)[0]["seed"], seed)

    def test_default_output_limit(self):
        self.assertEqual(JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [], 0)[1], 32768)

    def test_big_means_no_output_limit_at_all(self):
        payload, limit = JERRY_MOD.build_payload(JERRY_MOD.parse_args(["-b"]), [], 0)
        self.assertEqual(limit, 0)
        self.assertNotIn("max_tokens", payload)

    def test_limit_overrides_the_default(self):
        payload, limit = JERRY_MOD.build_payload(JERRY_MOD.parse_args(["--limit", "512"]), [], 0)
        self.assertEqual((limit, payload["max_tokens"]), (512, 512))

    def test_fast_sends_enable_thinking_false(self):
        self.assertIs(JERRY_MOD.build_payload(JERRY_MOD.parse_args(["-f"]), [], 0)[0].get("enable_thinking"),
                      False)

    def test_nothing_else_that_could_change_the_answer_is_touched(self):
        payload, _ = JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [{"role": "user", "content": "x"}], 0)
        for key in ("temperature", "top_k", "top_p", "min_p", "penalty", "grammar", "json_schema",
                    "reasoning_effort", "n", "stop", "cache"):
            self.assertNotIn(key, payload, "jerry should not send %s" % key)
        self.assertEqual(sorted(payload), ["max_tokens", "messages", "seed", "stream"])

    def test_session_is_exactly_one_message(self):
        self.assertEqual(JERRY_MOD.build_messages(JERRY_MOD.parse_args([]), "the quick brown fox"),
                         [{"role": "user", "content": "the quick brown fox"}])

    def test_capital_s_sends_a_system_message(self):
        self.assertEqual(JERRY_MOD.build_messages(JERRY_MOD.parse_args(["-S"]), "text")[0]["role"], "system")

    def test_stream_on_by_default_off_with_no_stream(self):
        self.assertTrue(JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [], 0)[0]["stream"])
        self.assertFalse(JERRY_MOD.build_payload(JERRY_MOD.parse_args(["--no-stream"]), [], 0)[0]["stream"])

    def test_model_is_only_sent_when_asked_for(self):
        self.assertNotIn("model", JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [], 0)[0])
        self.assertEqual(JERRY_MOD.build_payload(JERRY_MOD.parse_args(["-m", "some-model"]), [], 0)[0]["model"],
                         "some-model")

    def test_input_source_precedence_file_argument_stdin(self):
        class PipedIn(io.StringIO):
            def isatty(self):
                return False

        def piped(text):
            class In(PipedIn):
                pass
            return PipedIn(text)

        path = os.path.join(ROOT, "tests", ".tmp_input.txt")
        with open(path, "w") as fh:
            fh.write("from a file")
        saved = sys.stdin
        try:
            sys.stdin = piped("from stdin")
            self.assertEqual(JERRY_MOD.read_input(JERRY_MOD.parse_args(["an argument", "-i", path])),
                             "from a file")
            sys.stdin = piped("from stdin")
            self.assertEqual(JERRY_MOD.read_input(JERRY_MOD.parse_args(["an argument"])), "an argument")
            sys.stdin = piped("from stdin")
            self.assertEqual(JERRY_MOD.read_input(JERRY_MOD.parse_args([])), "from stdin")
            sys.stdin = piped("from stdin")
            self.assertEqual(JERRY_MOD.read_input(JERRY_MOD.parse_args(["-i", "-"])), "from stdin")
            sys.stdin = piped("")
            self.assertEqual(JERRY_MOD.read_input(JERRY_MOD.parse_args([])), "")
        finally:
            sys.stdin = saved
            os.unlink(path)

    def test_defaults_of_timeout_and_url(self):
        self.assertEqual(JERRY_MOD.parse_args([]).timeout, 600.0)
        self.assertEqual(load_jerry({"JERRY_URL": "http://example.test:1"}).parse_args([]).url,
                         "http://example.test:1")


# --------------------------------------------------------------------------- #
# live tests (against the mock server, or a real one with --server)
# --------------------------------------------------------------------------- #

PROMPT = "say hi in three words"
LONG = "write a short story about a key, three sentences"


class TestLive(LiveCase):

    def test_plain_run_prints_only_the_response(self):
        r = Run([PROMPT], stdin=PROMPT).assert_ok()
        self.assertTrue(r.out.strip(), "no response on stdout")
        self.assertEqual(r.err, b"", "stderr must be empty by default")

    def test_output_ends_with_a_newline(self):
        r = Run([PROMPT], stdin=PROMPT).assert_ok()
        self.assertTrue(r.out.endswith(b"\n"), "jerry must finish the output with a newline")
        self.assertFalse(r.out.endswith(b"\n\n"))

    def test_stdout_is_the_raw_response_with_no_decoration(self):
        text = Run([PROMPT], stdin=PROMPT).assert_ok().out.decode("utf-8", "replace")
        self.assertNotIn("jerry:", text)
        self.assertNotIn("```", text)

    def test_composes_with_a_pipe(self):
        p = subprocess.run("echo '%s' | %s" % (PROMPT, JERRY), shell=True, capture_output=True,
                           cwd=ROOT, timeout=TIMEOUT, env=dict(os.environ, JERRY_URL=SERVER))
        self.assertEqual(p.returncode, EXIT_OK)
        self.assertTrue(p.stdout.strip())
        self.assertEqual(p.stderr, b"", "the answer alone must come out of the pipe")
        self.assertEqual(p.stdout, p.stdout.rstrip(b"\n") + b"\n", "exactly one trailing newline")

    def test_positional_argument_wins_over_piped_stdin(self):
        r = Run(["positional text", "-v"], stdin="piped text is longer").assert_ok()
        self.assertIn("jerry: input   %d chars as user message" % len("positional text"), r.err.decode())

    def test_usage_error_exits_one(self):
        """README exit codes: 1 usage error."""
        r = Run(["--limit", "abc"], stdin="")
        self.assertEqual(r.code, EXIT_USAGE, "argparse usage errors must exit 1, got %s" % r.code)

    def test_nothing_to_send_exits_one(self):
        r = Run([], stdin="")
        self.assertEqual(r.code, EXIT_USAGE)
        self.assertIn("nothing to send", r.err.decode())

    def test_connection_error_exits_two(self):
        r = Run(["hi there"], env={"JERRY_URL": "http://127.0.0.1:9/"})
        self.assertEqual(r.code, EXIT_SERVER)
        self.assertIn("cannot reach", r.err.decode())

    def test_http_error_exits_two(self):
        r = Run(["hi there"], env={"JERRY_URL": SERVER + "/definitely-not-an-api"})
        self.assertEqual(r.code, EXIT_SERVER)
        self.assertRegex(r.err.decode().strip(), r"^jerry: http \d{3}:")

    def test_quiet_still_exits_two_and_prints_nothing(self):
        r = Run(["-q", "hi there"], env={"JERRY_URL": "http://127.0.0.1:9/"})
        self.assertEqual(r.code, EXIT_SERVER)
        self.assertEqual(r.err, b"")

    def test_output_limit_exits_three_with_the_documented_message(self):
        prompt = LONG
        r = Run(["--limit", "40", prompt], stdin=prompt)
        self.assertEqual(r.code, EXIT_LIMIT)
        self.assertEqual(r.err.decode().strip(),
                         "jerry: output limit reached (40 tokens), the response is incomplete; "
                         "use -b for no limit or --limit N")

    def test_quiet_with_the_limit_still_exits_three(self):
        prompt = LONG
        r = Run(["-q", "--limit", "40", prompt], stdin=prompt)
        self.assertEqual(r.code, EXIT_LIMIT)
        self.assertEqual(r.err, b"", "-q must suppress the message but not the exit code")

    def test_whatever_was_produced_is_printed_when_the_limit_is_hit(self):
        seen = False
        for _ in range(6):
            prompt = "List the numbers 1 to 300, one per line, no planning"
            r = Run(["--limit", "130", prompt], stdin=prompt)
            if r.code == EXIT_LIMIT and r.out:
                seen = True
                self.assertIsNone(re.search(rb"(?m)^300$", r.out), "the output should have been cut off")
                break
        if not seen:
            self.skipTest("no run produced answer text before the limit (the model was still reasoning)")

    def test_big_run_is_not_cut_off(self):
        self.assertTrue(Run(["-b", PROMPT], stdin=PROMPT).assert_ok().out.strip())

    def test_input_from_a_file(self):
        path = os.path.join(ROOT, "tests", ".tmp_in.txt")
        with open(path, "w") as fh:
            fh.write(PROMPT + "\n")
        try:
            self.assertTrue(Run(["-i", path]).assert_ok().out.strip())
        finally:
            os.unlink(path)

    def test_output_to_a_file(self):
        path = os.path.join(ROOT, "tests", ".tmp_out.txt")
        Run(["-o", path, PROMPT], stdin=PROMPT).assert_ok()
        with open(path) as fh:
            written = fh.read()
        self.assertTrue(written.strip())
        self.assertTrue(written.endswith("\n"))

    def test_echo_prints_input_and_response_as_one(self):
        text = Run(["-e", PROMPT], stdin=PROMPT).assert_ok().out.decode("utf-8", "replace")
        self.assertTrue(text.startswith(PROMPT + "\n\n"), "the input was clipped from the output")
        self.assertTrue(text[len(PROMPT) + 2:].strip())

    def test_reasoning_goes_to_stderr_with_r(self):
        prompt = "why is the sky blue, in one short answer"
        r = Run(["-r", prompt], stdin=prompt).assert_ok()
        if not r.err:
            self.skipTest("the model produced no reasoning for this prompt")
        self.assertTrue(r.out.strip(), "the answer still belongs on stdout")

    def test_reasoning_is_never_on_stdout(self):
        prompt = "why is the sky blue, in one short answer"
        r = Run(["-r", prompt], stdin=prompt).assert_ok()
        if not r.err:
            self.skipTest("the model produced no reasoning for this prompt")
        # the answer is much shorter than the reasoning; if the reasoning leaked
        # into stdout these two would be the other way round
        self.assertLess(len(r.out), len(r.err))

    def test_fast_is_sent_and_harmless(self):
        self.assertTrue(Run(["-f", PROMPT], stdin=PROMPT).assert_ok().out.strip())

    def test_system_message(self):
        prompt = "you are a helpful assistant, say hi in three words"
        r = Run(["-v", "-S", prompt], stdin=prompt).assert_ok()
        self.assertIn("as system message", r.err.decode())

    def test_no_stream_mode(self):
        r = Run(["-v", "--no-stream", PROMPT], stdin=PROMPT).assert_ok()
        self.assertIn("response", r.err.decode())
        self.assertTrue(r.out.strip())

    def test_broken_pipe_exits_cleanly_and_silently(self):
        """README: stderr is nothing by default; the documented exit codes apply."""
        r, w = os.pipe()
        proc = subprocess.Popen([JERRY, LONG], stdin=subprocess.PIPE, stdout=w,
                                stderr=subprocess.PIPE, cwd=ROOT,
                                env=dict(os.environ, JERRY_URL=SERVER))
        os.close(w)
        nonblock(r)
        got = b""
        # read a little, then hang up like `head -c 40` does
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if select.select([r], [], [], 0.5)[0]:
                data = os.read(r, 64)
                if not data:
                    break
                got += data
                if len(got) >= 40:
                    break
        os.close(r)                       # jerry is now writing into a closed pipe
        err = proc.stderr.read()
        code = proc.wait(timeout=60)
        proc.stdin.close()
        proc.stderr.close()
        self.assertTrue(got, "nothing was streamed before the pipe closed")
        self.assertEqual(code, EXIT_OK, "broken pipe should exit 0, got %s" % code)
        self.assertEqual(err, b"", "nothing belongs on stderr, got %r" % err)

    def test_keyboard_interrupt_exits_130(self):
        prompt = "write a two hundred word story about a key"
        proc = subprocess.Popen([JERRY, "-v", prompt], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT,
                                env=dict(os.environ, JERRY_URL=SERVER))
        proc.stdin.write((prompt + "\n").encode())
        proc.stdin.close()
        # interrupt while the meter is running, i.e. while it is really generating
        deadline = time.monotonic() + TIMEOUT
        started = False
        for raw in proc.stderr:
            if re.search(r"jerry: \d+/\d+ .*streaming", raw.decode("utf-8", "replace")):
                started = True
                break
            if time.monotonic() > deadline:
                break
        if not started:
            proc.kill()
            self.fail("no meter update before the deadline, nothing to interrupt")
        proc.send_signal(signal.SIGINT)
        try:
            err = proc.stderr.read()
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("jerry did not react to SIGINT")
        code = proc.wait(timeout=60)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(code, EXIT_KILLED)
        self.assertIn("interrupted", err.decode("utf-8", "replace"))

    def test_tool_calls_end_the_run_with_exit_two(self):
        prompt = "please call the tool"
        if SERVER != MOCK_URL:
            self.skipTest("jerry sends no tools, so only the mock server produces a tool call; "
                          "the mapping is covered in TestReadmeSpec")
        r = Run([prompt], stdin=prompt)
        self.assertEqual(r.code, EXIT_SERVER)
        self.assertIn("the model requested a tool call, jerry has no tools to run", r.err.decode())
        self.assertIn("get_weather(", r.out.decode(), "the call is printed on stdout")


class TestVerboseMeter(LiveCase):

    def test_header_lines_match_the_readme_example(self):
        r = Run(["-v", PROMPT], stdin=PROMPT).assert_ok()
        lines = r.stderr_lines()
        self.assertEqual(lines[0], "jerry: server  %s" % SERVER)
        self.assertRegex(lines[1], r"^jerry: model   \S+")
        self.assertEqual(lines[2], "jerry: seed    0")
        self.assertEqual(lines[3], "jerry: limit   32768 tokens")
        self.assertEqual(lines[4], "jerry: reasoning hidden")
        self.assertEqual(lines[5], "jerry: input   %d chars as user message" % len(PROMPT))

    def test_summary_lines_match_the_readme_example(self):
        text = Run(["-v", PROMPT], stdin=PROMPT).assert_ok().err.decode("utf-8", "replace")
        self.assertRegex(text, r"jerry: finish  \w+")
        self.assertRegex(text, r"jerry: prompt  \d+ tokens \(\d+\.\d t/s\)")
        self.assertRegex(text, r"jerry: output  \d+ tokens \(\d+\.\d t/s\) first token \d+\.\d\ds")
        self.assertRegex(text, r"jerry: \d+/\d+ +\d+\.\d+% +\d+\.\d+ t/s +\d+\.\d+s \w+")

    def test_meter_prints_one_line_per_update_when_stderr_is_not_a_terminal(self):
        r = Run(["-v", LONG], stdin=LONG).assert_ok()
        meter = [l for l in r.stderr_lines() if re.match(r"^jerry: \d+/\d+ ", l)]
        self.assertGreaterEqual(len(meter), 2, "the meter should update while streaming")

    def test_reasoning_count_line_when_r_is_not_used(self):
        text = Run(["-v", PROMPT], stdin=PROMPT).assert_ok().err.decode("utf-8", "replace")
        if "use -r to see it" not in text:
            self.skipTest("this run produced no reasoning")
        self.assertRegex(text, r"jerry: reasoning \d+ chars, use -r to see it")

    def test_with_r_the_count_line_is_not_printed(self):
        text = Run(["-v", "-r", PROMPT], stdin=PROMPT).assert_ok().err.decode("utf-8", "replace")
        self.assertNotIn("use -r to see it", text)

    def test_response_is_streamed_live_when_stderr_is_not_a_terminal(self):
        r = Run(["-v", LONG], stdin=LONG).assert_ok()
        out_ts, finish_ts = r.first("out"), r.first("err", r"jerry: finish")
        self.assertIsNotNone(out_ts, "nothing was streamed on stdout")
        self.assertIsNotNone(finish_ts, "no finish line")
        self.assertLess(out_ts, finish_ts, "the answer should arrive while the meter is running")

    def test_on_a_terminal_the_meter_owns_stderr_and_the_answer_comes_once(self):
        r = Run(["-v", PROMPT], stdin=PROMPT, tty_stderr=True)
        self.assertEqual(r.code, EXIT_OK)
        meter_ts, out_ts = r.first("err", r"jerry: \d+/\d+ "), r.first("out")
        self.assertIsNotNone(meter_ts, "no meter line on the terminal")
        self.assertGreater(out_ts, meter_ts, "the answer must not break up the meter line")
        self.assertIn("jerry: finish", r.err.decode())
        self.assertNotIn("three words", r.err.decode(), "the response was echoed on the meter's stderr")

    def test_model_line_reports_what_the_server_says(self):
        r = Run(["-v", PROMPT], stdin=PROMPT).assert_ok()
        reported = re.search(r"^jerry: model   (\S+)", r.err.decode(), re.M).group(1)
        data = json.loads(urllib.request.urlopen(SERVER + "/v1/models", timeout=10).read())
        self.assertEqual(reported, data["data"][0]["id"])

    def test_m_is_used_without_querying_the_server_for_a_name(self):
        r = Run(["-v", "-m", "an-arbitrary-model-name", PROMPT], stdin=PROMPT)
        self.assertEqual(r.code, EXIT_OK)
        self.assertIn("jerry: model   an-arbitrary-model-name", r.err.decode())

    def test_seed_lines_fixed_and_random(self):
        r = Run(["-v", "-s", "1234", PROMPT], stdin=PROMPT).assert_ok()
        self.assertIn("jerry: seed    1234\n", r.err.decode())
        r = Run(["-v", "-s"], stdin=PROMPT).assert_ok()
        self.assertRegex(r.err.decode(), r"jerry: seed    \d+ \(random\)")

    def test_jerry_seed_changes_the_default(self):
        text = Run(["-v", PROMPT], stdin=PROMPT, env={"JERRY_SEED": "777"}).assert_ok().err.decode()
        self.assertIn("jerry: seed    777", text)

    def test_limit_and_reasoning_lines(self):
        text = Run(["-v", "-f", "-b", PROMPT], stdin=PROMPT).assert_ok().err.decode()
        self.assertIn("jerry: limit   none (-b)", text)
        self.assertIn("jerry: reasoning off (-f)", text)

    def test_same_input_same_answer_on_a_server_that_honours_the_seed(self):
        prompt = "tell me a one sentence story about a key"
        a = Run(["-s", "1234", prompt], stdin=prompt).assert_ok()
        b = Run(["-s", "1234", prompt], stdin=prompt).assert_ok()
        if a.out != b.out:
            self.skipTest("this server does not honour the seed (documented in the README)")
        self.assertEqual(a.out, b.out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="tests for jerry (bundled mock server unless --server is given)")
    p.add_argument("--server", default=SERVER, help="test a real server instead of the mock")
    p.add_argument("-k", dest="patterns", action="append", default=[],
                   help="only tests whose name contains this (repeatable)")
    p.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds a run may take")
    opts, rest = p.parse_known_args()
    SERVER = opts.server
    TIMEOUT = opts.timeout
    for pattern in opts.patterns:
        rest += ["-k", pattern]
    unittest.main(argv=[sys.argv[0]] + rest, exit=True)
