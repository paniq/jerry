#!/usr/bin/env python3
"""Tests for jerry and tom.

Every test checks a behaviour documented in ../README.md. By default the suite
starts the mock server in mock_server.py on a free port and talks to that, so it
needs no model and takes seconds. To check a real llama.cpp server instead, point
the suite at it; tests that need a server are skipped if it does not answer, and
the ones whose proof is a canned mock answer skip themselves against a real model.

    python3 tests/test_jerry.py                  # bundled mock server on a free port
    python3 tests/test_jerry.py --server URL     # a real server (or JERRY_TEST_URL)
    python3 tests/test_jerry.py -k broken_pipe   # run a subset (matches the test name)

Against a real server the run time is dominated by the model, and some tests skip
themselves when the model does not produce what they need (reasoning, a seeded
repeat); the mock always produces it.
"""

import argparse
import base64
import contextlib
import fcntl
import inspect
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
import typing
import unittest
import urllib.request
from importlib import machinery as importlib_machinery
from importlib import util as importlib_util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JERRY = os.path.join(ROOT, "jerry")
TOM = os.path.join(ROOT, "tom")
README = os.path.join(ROOT, "README.md")
TOOLS_MD = os.path.join(ROOT, "TOOLS.md")
TOOLMOD = os.path.join(HERE, "toolmod.py")

sys.path.insert(0, HERE)
import mock_server  # noqa: E402

# None means "start the mock server" (see setUpModule). JERRY_URL is deliberately not
# consulted: a stray JERRY_URL must not make the suite talk to somebody's real server.
SERVER = os.environ.get("JERRY_TEST_URL") or None
TIMEOUT = float(os.environ["JERRY_TEST_TIMEOUT"]) if os.environ.get("JERRY_TEST_TIMEOUT") else None

EXIT_OK, EXIT_USAGE, EXIT_SERVER, EXIT_LIMIT, EXIT_TOOL, EXIT_KILLED = 0, 1, 2, 3, 4, 130


def doc_flags(path, heading):
    """Flags documented in a table under the given heading of a doc file."""
    with open(path) as fh:
        text = fh.read()
    section = text.split(heading, 1)[1].split("\n---", 1)[0]
    return re.findall(r"^\| `(-{1,2}[A-Za-z-]+)", section, re.M)


def readme_flags(heading):
    """Flags documented in a README table under the given heading."""
    return doc_flags(README, heading)


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
    return load_module("jerry_under_test", JERRY, env)


def load_tom(env=None):
    """Import tom as a module, same trick as load_jerry."""
    return load_module("tom_under_test", TOM, env)


def load_module(name, path, env=None):
    saved = dict(os.environ)
    try:
        if env:
            os.environ.update(env)
        loader = importlib_machinery.SourceFileLoader(name, path)
        spec = importlib_util.spec_from_loader(name, loader)
        mod = importlib_util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


JERRY_MOD = load_jerry()
TOM_MOD = load_tom()


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


def run_tom(argv, stdin="", env=None):
    """One run of tom as a subprocess (so the exit codes, --help and the pipes are real)."""
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run([TOM] + list(argv), input=(stdin or "").encode(), capture_output=True,
                          cwd=ROOT, timeout=TIMEOUT, env=merged)


def tom_then_jerry(tom_args, jerry_args=(), stdin=""):
    """Run tom, feed what it produced to jerry. -> (tom run, jerry run)"""
    t = run_tom(tom_args, stdin)
    if t.returncode != EXIT_OK:
        return t, None
    j = subprocess.run([JERRY] + list(jerry_args), input=t.stdout, capture_output=True,
                       cwd=ROOT, timeout=TIMEOUT, env=dict(os.environ, JERRY_URL=SERVER))
    return t, j


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
        for code in (0, 1, 2, 3, 4, 130):
            self.assertIn(" %d " % code, r.stdout, "help should list exit code %d" % code)

    def test_help_lists_every_documented_flag(self):
        flags = readme_flags("## Options")
        self.assertTrue(flags, "no flags found in the README option table")
        r = subprocess.run([JERRY, "--help"], capture_output=True, text=True)
        for flag in flags:
            self.assertIn(flag, r.stdout, "%s is documented in the README but missing from --help" % flag)

    def test_tool_flags_are_documented_in_tools_md_and_in_help(self):
        """TOOLS.md holds the tool documentation, so its table has to match --help too."""
        flags = doc_flags(TOOLS_MD, "## Options")
        self.assertEqual(sorted(flags), ["--max-turns", "-L", "-T"], flags)
        r = subprocess.run([JERRY, "--help"], capture_output=True, text=True)
        for flag in flags:
            self.assertIn(flag, r.stdout, "%s is documented in TOOLS.md but missing from --help" % flag)
        self.assertIn("TOOLS.md", r.stdout, "--help should point at the tool documentation")

    def test_documented_defaults(self):
        r = subprocess.run([JERRY, "--help"], capture_output=True, text=True)
        self.assertIn("http://localhost:9931", r.stdout)
        self.assertIn("http://localhost:8080", r.stdout)      # documented as the fallback
        self.assertIn("32768", r.stdout)
        self.assertIn("600", r.stdout)

    def test_stock_python3_standard_library_only(self):
        for path in (JERRY, TOM):
            with open(path) as fh:
                src = fh.read()
            self.assertEqual(src.splitlines()[0], "#!/usr/bin/env python3")
            mods = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)", src, re.M))
            stdlib = set(sys.stdlib_module_names)
            for mod in mods:
                self.assertIn(mod.split(".")[0], stdlib,
                              "%s is not standard library" % mod)

    def test_no_client_side_response_cache(self):
        for path in (JERRY, TOM):
            with open(path) as fh:
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
        args = JERRY_MOD.parse_args([])
        meter = JERRY_MOD.Meter(args, 100)
        meter.tty = meter.clean = True          # the meter owns stderr, so output is held
        buf = io.StringIO()
        writer = JERRY_MOD.Writer(args, meter, io.StringIO())
        writer.out_fh, writer.err_fh = buf, buf
        writer.write("out", "answer")
        self.assertEqual(buf.getvalue(), "", "held until the run is over")
        writer.flush()
        self.assertEqual(buf.getvalue(), "answer")
        writer.flush()
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

    def test_a_tool_call_is_not_an_error_when_there_are_tools_to_run(self):
        registry = JERRY_MOD.build_registry(JERRY_MOD.parse_args(["-T", TOOLMOD]))
        with captured_stderr() as buf:
            code = JERRY_MOD.finish_code(JERRY_MOD.parse_args([]), "tool_calls", 32768, registry)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(buf.getvalue(), "", "the call is run, not reported")

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
# tom: the tag writer
# --------------------------------------------------------------------------- #

def tom_payloads(text):
    """Payloads inside tom's output, in the order they were declared."""
    return [m.group(2) for m in re.finditer(r"<jerry:([0-9a-f]{12})>(.*?)</jerry:\1>", text, re.DOTALL)]


def tom_ids(text):
    return re.findall(r"<jerry:([0-9a-f]{12})>", text)


class TestTom(unittest.TestCase):

    def test_tag_pair_is_opened_and_closed_with_the_same_id(self):
        out = run_tom(["tests/red.png"]).stdout.decode()
        self.assertRegex(out, r"(?s)^<jerry:([0-9a-f]{12})>.*?</jerry:\1>\n$", "tom must write a matched pair")

    def test_tag_ids_are_random_but_always_twelve_hex_chars(self):
        ids = tom_ids(run_tom(["tests/red.png"]).stdout.decode())
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(ids[0]), 12)
        self.assertNotEqual(ids, tom_ids(run_tom(["tests/red.png"]).stdout.decode()))

    def test_text_files_are_passed_verbatim_inside_the_tags(self):
        path = os.path.join(ROOT, "tests", ".tmp_note.md")
        with open(path, "w") as fh:
            fh.write("the quick brown fox\n")
        try:
            out = run_tom([path]).stdout.decode()
            self.assertIn("the quick brown fox", out)
            self.assertNotIn("base64", out, "a .md file must not be encoded")
        finally:
            os.unlink(path)

    def test_other_files_are_base64_with_a_data_uri_prefix(self):
        payload = tom_payloads(run_tom(["tests/red.png"]).stdout.decode())[0]
        self.assertTrue(payload.startswith("data:image/png;base64,"), payload[:40])
        with open(os.path.join(ROOT, "tests", "red.png"), "rb") as fh:
            self.assertEqual(base64.b64decode(payload.split(",", 1)[1]), fh.read(),
                             "the payload must decode back to the file")

    def test_mime_type_comes_from_the_extension(self):
        cases = {("tests/blue.jpg", "image/jpeg"), ("tests/red.png", "image/png"),
                 ("tests/.tmp_unknown.bin", "application/octet-stream")}
        path = os.path.join(ROOT, "tests", ".tmp_unknown.bin")
        with open(path, "wb") as fh:
            fh.write(b"not an image")
        try:
            for file, mime in cases:
                payload = tom_payloads(run_tom([file]).stdout.decode())[0]
                self.assertTrue(payload.startswith("data:%s;base64," % mime),
                                "%s should be %s, got %s" % (file, mime, payload[:30]))
        finally:
            os.unlink(path)

    def test_stdin_passes_through_untagged_at_its_position(self):
        md = os.path.join(ROOT, "tests", ".tmp_a.md")
        bin_ = os.path.join(ROOT, "tests", ".tmp_b.bin")
        with open(md, "w") as fh:
            fh.write("the quick brown fox\n")
        with open(bin_, "wb") as fh:
            fh.write(b"not an image")
        try:
            out = run_tom([md, "-", bin_], stdin="piped text").stdout.decode()
            pairs = [(m.group(2), m.start(), m.end())
                     for m in re.finditer(r"<jerry:([0-9a-f]{12})>(.*?)</jerry:\1>", out, re.DOTALL)]
            self.assertEqual(len(pairs), 2, "the text file and the binary file, in that order")
            self.assertTrue(pairs[0][0].startswith("the quick brown fox"),
                            "a .md file is passed verbatim, not encoded")
            self.assertIn("piped text", out[pairs[0][2]:pairs[1][1]], "stdin must not be wrapped in a tag")
            self.assertTrue(pairs[1][0].startswith("data:application/octet-stream;base64,"),
                            "the binary file comes last")
        finally:
            os.unlink(md)
            os.unlink(bin_)

    def test_stdin_is_passed_through_byte_for_byte(self):
        """No extra newline: tom adds one after a file, not after stdin."""
        out = run_tom(["-"], stdin="two lines\nthen a blank\n\n").stdout.decode()
        self.assertEqual(out, "two lines\nthen a blank\n\n")

    def test_nothing_to_send_is_a_usage_error(self):
        r = run_tom([], stdin="piped text")
        self.assertEqual(r.returncode, EXIT_USAGE)
        self.assertIn("no files specified", r.stderr.decode())

    def test_unreadable_file_is_a_usage_error(self):
        r = run_tom(["definitely-not-here.png"], stdin="")
        self.assertEqual(r.returncode, EXIT_USAGE)
        self.assertIn("cannot read", r.stderr.decode())

    def test_stdin_can_only_be_read_once(self):
        r = run_tom(["-", "-"], stdin="piped text")
        self.assertEqual(r.returncode, EXIT_USAGE)
        self.assertIn("cannot appear twice", r.stderr.decode())

    def test_stdin_on_a_terminal_is_refused(self):
        class TtyIn(io.StringIO):
            def isatty(self):
                return True

        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin = TtyIn("piped text")
        try:
            with captured_stderr() as buf, contextlib.redirect_stdout(io.StringIO()):
                code = TOM_MOD.main(["-"])
        finally:
            sys.stdin, sys.stdout = saved_in, saved_out
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nothing piped on stdin", buf.getvalue())

    def test_quiet_suppresses_the_message_but_not_the_exit_code(self):
        r = run_tom(["definitely-not-here.png", "-q"], stdin="")
        self.assertEqual(r.returncode, EXIT_USAGE)
        self.assertEqual(r.stderr, b"")

    def test_broken_pipe_exits_cleanly_and_silently(self):
        r, w = os.pipe()
        os.close(r)                     # tom is now writing into a closed pipe
        proc = subprocess.Popen([TOM, "tests/red.png"], stdin=subprocess.DEVNULL, stdout=w,
                                stderr=subprocess.PIPE, cwd=ROOT)
        os.close(w)
        err = proc.stderr.read()
        code = proc.wait(timeout=30)
        proc.stderr.close()
        self.assertEqual(code, EXIT_OK, "broken pipe should exit 0, got %s" % code)
        self.assertEqual(err, b"", "nothing belongs on stderr, got %r" % err)

    def test_exit_codes_documented_in_help(self):
        r = subprocess.run([TOM, "--help"], capture_output=True, text=True)
        for code in (0, 1, 130):
            self.assertIn(" %d " % code, r.stdout, "help should list exit code %d" % code)

    def test_help_lists_every_documented_flag(self):
        flags = readme_flags("### tom options")
        self.assertTrue(flags, "no flags found in the tom table in the README")
        r = subprocess.run([TOM, "--help"], capture_output=True, text=True)
        for flag in flags:
            self.assertIn(flag, r.stdout, "%s is documented in the README but missing from --help" % flag)

    def test_version(self):
        r = subprocess.run([TOM, "--version"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "tom %s" % TOM_MOD.VERSION)


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
        mod = load_jerry({"JERRY_URL": "http://example.test:1"})
        self.assertEqual(mod.parse_args([]).url, "http://example.test:1")
        self.assertEqual(mod.parse_args([]).url_candidates, ["http://example.test:1"],
                         "a url from the environment or from the command line has no fallback")


# --------------------------------------------------------------------------- #
# what jerry makes of tom's tags
# --------------------------------------------------------------------------- #

def tagged(payload, tag=None):
    """A well formed <jerry:HEXD> pair around a payload."""
    tag = tag or "a" * 12
    return "<jerry:%s>%s</jerry:%s>" % (tag, payload, tag)


class TestMultimodalContent(unittest.TestCase):

    def test_input_without_tags_is_still_plain_string_content(self):
        self.assertIsNone(JERRY_MOD.parse_multimodal("plain text"))
        self.assertEqual(JERRY_MOD.build_messages(JERRY_MOD.parse_args([]), "plain text"),
                         [{"role": "user", "content": "plain text"}])

    def test_data_payloads_become_image_url_entries(self):
        self.assertEqual(JERRY_MOD.parse_multimodal(tagged("data:image/png;base64,AAA")),
                         [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}])

    def test_payloads_that_are_not_data_are_text(self):
        """tom writes .txt and .md files verbatim inside the tags."""
        self.assertEqual(JERRY_MOD.parse_multimodal(tagged("a note")),
                         [{"type": "text", "text": "a note"}])

    def test_text_between_and_after_tags_keeps_its_own_entry(self):
        entries = JERRY_MOD.parse_multimodal("before " + tagged("data:image/png;base64,AAA") + " after")
        self.assertEqual([e["type"] for e in entries], ["text", "image_url", "text"])
        self.assertEqual(entries[0]["text"], "before ")
        self.assertEqual(entries[-1]["text"], " after")

    def test_a_payload_spanning_lines_is_still_content(self):
        """tom passes a text file through verbatim, so a tag can contain newlines."""
        entries = JERRY_MOD.parse_multimodal(tagged("first line\nsecond line\n"))
        self.assertEqual(entries, [{"type": "text", "text": "first line\nsecond line\n"}])

    def test_nothing_is_emitted_for_an_empty_piece(self):
        entries = JERRY_MOD.parse_multimodal(tagged("data:image/png;base64,AAA") + tagged("data:image/gif;base64,BBB"))
        self.assertEqual(len(entries), 2, "two attachments, no empty text entry between them")
        self.assertEqual([e["type"] for e in entries], ["image_url", "image_url"])

    def test_order_of_the_attachments_is_kept(self):
        md = os.path.join(ROOT, "tests", ".tmp_order.md")
        with open(md, "w") as fh:
            fh.write("a note between two images\n")
        try:
            text = run_tom(["tests/red.png", md, "tests/blue.jpg"]).stdout.decode()
            entries = [e for e in JERRY_MOD.parse_multimodal(text)
                       if e["type"] == "image_url" or e["text"].strip()]
            self.assertEqual([e["type"] for e in entries], ["image_url", "text", "image_url"],
                             "the attachments are kept in the order tom was given them")
            self.assertIn("a note between two images", entries[1]["text"])
        finally:
            os.unlink(md)

    def test_unpaired_tags_are_sent_as_the_text_they_are(self):
        for text in (tagged("a") .replace("</jerry:" + "a" * 12, "</jerry:" + "b" * 12),
                     "<jerry:" + "a" * 12 + ">no closing tag",
                     "no opening tag</jerry:" + "a" * 12 + ">"):
            self.assertIsNone(JERRY_MOD.parse_multimodal(text), "%r is not a matched pair" % text)
            self.assertEqual(JERRY_MOD.build_messages(JERRY_MOD.parse_args([]), text)[0]["content"], text,
                             "a broken pair is sent as the text it is")

    def test_the_content_array_is_still_exactly_one_message(self):
        messages = JERRY_MOD.build_messages(JERRY_MOD.parse_args([]), tagged("data:image/png;base64,AAA"))
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0]["content"], list)

    def test_capital_s_still_decides_the_role_for_content_arrays(self):
        self.assertEqual(JERRY_MOD.build_messages(JERRY_MOD.parse_args(["-S"]),
                                                  tagged("data:image/png;base64,AAA"))[0]["role"], "system")

    def test_multimodal_adds_nothing_else_to_the_request(self):
        payload, _ = JERRY_MOD.build_payload(JERRY_MOD.parse_args([]),
                                             [{"role": "user", "content": [{"type": "text", "text": "x"}]}], 0)
        self.assertEqual(sorted(payload), ["max_tokens", "messages", "seed", "stream"])

    def test_the_content_array_carries_only_the_payloads(self):
        """jerry builds the content from the payload, so the request depends on the files
        alone: whatever ID tom picked, the same files send the same thing."""
        a = run_tom(["tests/red.png"], stdin="what is in this image?").stdout.decode()
        b = run_tom(["tests/red.png"], stdin="what is in this image?").stdout.decode()
        self.assertNotEqual(a, b, "tom picks a new id every run, that is the point")
        self.assertNotIn("<jerry:", json.dumps(JERRY_MOD.parse_multimodal(a)), "the request carries no tags")
        self.assertEqual(json.dumps(JERRY_MOD.parse_multimodal(a)),
                         json.dumps(JERRY_MOD.parse_multimodal(b)),
                         "the same files must send the same content")


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# tools: what a tool module becomes
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def quiet_run(log=None, verbose=True):
    """A run with nothing but a writer, so jerry's parts can be called directly."""
    args = JERRY_MOD.parse_args(["-v"] if verbose else [])
    meter = JERRY_MOD.Meter(args, 100)
    meter.tty = meter.clean = False
    writer = JERRY_MOD.Writer(args, meter, log)
    out, err = io.StringIO(), io.StringIO()
    writer.out_fh, writer.err_fh = out, err
    meter.stream = err                            # the meter writes to stderr as well
    saved = JERRY_MOD.ACTIVE
    JERRY_MOD.ACTIVE = writer
    try:
        yield args, writer, meter, out, err
    finally:
        JERRY_MOD.ACTIVE = saved


def registry_of(*paths):
    argv = []
    for path in paths:
        argv += ["-T", path]
    return JERRY_MOD.build_registry(JERRY_MOD.parse_args(argv))


def spec_of(name):
    """The metadata jerry derives for one tool in the fixture module."""
    return registry_of(TOOLMOD)[name][1]


def tool(name, arguments):
    """Run one tool through jerry, and return what the model was told."""
    with quiet_run(io.StringIO()) as (args, writer, meter, out, err):
        return JERRY_MOD.call_tool(args, writer, meter, registry_of(TOOLMOD), name, arguments, 1)


def temp_module(name, text):
    path = os.path.join(HERE, name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


class TestToolMetadata(unittest.TestCase):

    def test_every_function_beginning_with_tool_is_a_tool(self):
        module = JERRY_MOD.load_tool_module(TOOLMOD)
        self.assertEqual(sorted(registry_of(TOOLMOD)),
                         ["anything", "broken", "get_weather", "list_cities", "note", "prints", "silent",
                          "slow", "untyped"])
        self.assertIn("helper", vars(module), "a tool module may have helpers of its own")
        self.assertNotIn("helper", registry_of(TOOLMOD), "a name that does not begin with tool_ is not a tool")
        self.assertTrue(callable(vars(module)["helper"]))

    def test_the_tool_name_is_the_function_name_without_the_prefix(self):
        self.assertEqual(spec_of("get_weather")["name"], "get_weather")
        self.assertEqual(registry_of(TOOLMOD)["get_weather"][0].__name__, "tool_get_weather",
                         "the prefix is what marks a function as a tool")

    def test_the_declaration_becomes_the_json_rpc_metadata(self):
        """The example in the spec: everything is read off the function itself."""
        spec = spec_of("get_weather")
        self.assertEqual(spec["description"], "Current weather for a city.")
        self.assertEqual(spec["parameters"]["type"], "object")
        self.assertEqual(sorted(spec["parameters"]["properties"]), ["city", "units"])
        self.assertEqual(spec["parameters"]["properties"]["city"],
                         {"type": "string", "description": "city name"})
        self.assertEqual(spec["parameters"]["required"], ["city"], "units has a default")
        self.assertEqual(spec["parameters"]["properties"]["units"]["default"], "c")

    def test_no_description_and_no_parameters_is_still_a_tool(self):
        spec = spec_of("list_cities")
        self.assertEqual(spec["description"], "Cities known to the server.")
        self.assertEqual(spec["parameters"]["properties"], {})
        self.assertEqual(spec["parameters"]["required"], [])
        self.assertEqual(json.loads(tool("list_cities", "{}")), ["Paris", "Oslo"])

    def test_a_tool_that_returns_nothing_answers_no_result(self):
        self.assertEqual(tool("silent", "{}"), "no result")

    def test_annotations_become_json_schema_types(self):
        want = {"broken": {"city": {"type": "string", "description": "city name"}},
                "note": {"name": {"type": "string", "description": "the name"},
                         "note": {"type": "string",
                                  "description": "left out when there is nothing to add",
                                  "default": None}},
                "untyped": {"value": {"description": "what to say"}},
                "anything": {"name": {"type": "string",
                                      "description": "the only parameter that is a parameter"}}}
        for name, properties in want.items():
            self.assertEqual(spec_of(name)["parameters"]["properties"], properties, name)

    def test_the_shape_of_an_annotation_is_kept(self):
        self.assertEqual(JERRY_MOD.json_type(list[str]), {"type": "array", "items": {"type": "string"}})
        self.assertEqual(JERRY_MOD.json_type(dict[str, int]),
                         {"type": "object", "additionalProperties": {"type": "integer"}})
        self.assertEqual(JERRY_MOD.json_type(int), {"type": "integer"})
        self.assertEqual(JERRY_MOD.json_type(bool), {"type": "boolean"})
        self.assertEqual(JERRY_MOD.json_type(str | None), {"anyOf": [{"type": "string"}, {"type": "null"}]})
        self.assertEqual(sorted(JERRY_MOD.json_type(typing.Literal["c", "f"]).items()),
                         [("enum", ["c", "f"]), ("type", "string")])
        self.assertEqual(JERRY_MOD.json_type(inspect.Parameter.empty), {})

    def test_star_args_are_not_parameters(self):
        spec = spec_of("anything")
        self.assertEqual(sorted(spec["parameters"]["properties"]), ["name"])
        self.assertEqual(spec["parameters"]["required"], ["name"])

    def test_an_optional_parameter_is_not_required_but_is_reported(self):
        spec = spec_of("note")
        self.assertEqual(spec["parameters"]["required"], ["name"], "note has a default")
        self.assertIn("note", spec["parameters"]["properties"])

    def test_a_tool_can_be_a_function_the_module_only_reexports(self):
        path = temp_module(".tmp_reexport.py", "def helper(x):\n    return x\n\ntool_alias = helper\n")
        try:
            self.assertEqual(sorted(registry_of(path)), ["alias"],
                              "every name that begins with tool_ is a tool, however it got there")
        finally:
            os.unlink(path)

    def test_a_module_with_no_tool_functions_is_a_usage_error(self):
        path = temp_module(".tmp_notools.py", "def nothing():\n    return 1\n")
        try:
            with self.assertRaises(JERRY_MOD.UsageError):
                registry_of(path)
        finally:
            os.unlink(path)

    def test_two_modules_that_define_the_same_tool_are_a_usage_error(self):
        path = temp_module(".tmp_same.py", "def tool_get_weather(x: str) -> str:\n    return x\n")
        try:
            with self.assertRaises(JERRY_MOD.UsageError) as caught:
                registry_of(TOOLMOD, path)
            self.assertIn("both define tool get_weather", str(caught.exception))
        finally:
            os.unlink(path)

    def test_an_unreadable_tool_module_is_a_usage_error(self):
        with self.assertRaises(JERRY_MOD.UsageError):
            registry_of("tests/definitely-not-here.py")

    def test_a_module_that_raises_while_it_is_being_loaded_is_a_usage_error(self):
        path = temp_module(".tmp_broken_import.py", "raise RuntimeError('the module itself fails')\n")
        try:
            with self.assertRaises(JERRY_MOD.UsageError):
                registry_of(path)
        finally:
            os.unlink(path)

    def test_the_tools_are_sent_in_name_order(self):
        registry = registry_of(TOOLMOD)
        payload, _ = JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [], 0, registry)
        self.assertEqual([t["function"]["name"] for t in payload["tools"]], sorted(registry))
        self.assertEqual(payload["tools"][0]["type"], "function")

    def test_no_tools_are_sent_when_nothing_was_given(self):
        payload, _ = JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [], 0)
        self.assertNotIn("tools", payload)

    def test_the_tools_are_the_only_thing_added_for_a_tool_module(self):
        payload, _ = JERRY_MOD.build_payload(JERRY_MOD.parse_args([]), [], 0, registry_of(TOOLMOD))
        self.assertEqual(sorted(payload), ["max_tokens", "messages", "seed", "stream", "tools"])

    def test_a_call_that_arrives_in_pieces_is_built_up(self):
        args = JERRY_MOD.parse_args([])
        meter = JERRY_MOD.Meter(args, 100)
        meter.tty = meter.clean = False
        writer = JERRY_MOD.Writer(args, meter, None)
        writer.out_fh, writer.err_fh = io.StringIO(), io.StringIO()

        data = JERRY_MOD.new_data()
        for piece in ('{"city": ', '"Paris"}'):
            JERRY_MOD.take(args, {"tool_calls": [{"index": 0, "id": "call_1",
                                                  "function": {"name": "get_weather",
                                                             "arguments": piece}}]}, data, meter, writer)
        self.assertEqual(data["tool_calls"][0]["arguments"], '{"city": "Paris"}')
        self.assertEqual(data["tool_calls"][0]["name"], "get_weather")

        # an argument sent as an object instead of a string reads the same way
        other = JERRY_MOD.new_data()
        JERRY_MOD.take(args, {"tool_calls": [{"function": {"name": "get_weather",
                                                          "arguments": {"city": "Paris"}}}]}, other, meter, writer)
        self.assertEqual(other["tool_calls"][0]["arguments"], '{"city": "Paris"}')

    def test_the_result_of_a_tool_is_json_when_it_is_not_text(self):
        self.assertEqual(JERRY_MOD.tool_text("a string"), "a string")
        self.assertEqual(JERRY_MOD.tool_text(["Paris", "Oslo"]), '["Paris", "Oslo"]')
        self.assertEqual(JERRY_MOD.tool_text({"city": "Paris"}), '{"city": "Paris"}')
        self.assertEqual(JERRY_MOD.tool_text(None), "no result", "a tool that only printed")

    def test_a_failure_is_fed_back_to_the_model_as_the_answer_to_its_call(self):
        self.assertEqual(tool("broken", '{"city": "Paris"}'),
                         "tool broken failed: ValueError: the weather service is down")

    def test_arguments_that_are_not_json_are_fed_back_as_well(self):
        self.assertIn("not valid json", tool("get_weather", '{city":'))
        self.assertIn("not an object", tool("get_weather", "[1, 2]"))

    def test_a_tool_name_the_module_does_not_have_is_fed_back_as_well(self):
        self.assertEqual(tool("translate", "{}"), "no tool named translate")

    def test_a_tool_prints_in_the_order_it_spoke(self):
        """The call line, whatever the tool printed while it ran, then the result line."""
        with quiet_run(io.StringIO()) as (args, writer, meter, out, err):
            writer.hold = True                  # record the events instead of writing them
            JERRY_MOD.call_tool(args, writer, meter, registry_of(TOOLMOD), "prints", '{"note": "hi"}', 1)
            events = [(channel, text) for channel, text in writer.pending if text.strip()]
            self.assertEqual([channel for channel, _ in events], ["err", "out", "err", "err", "log"])
            self.assertIn("jerry: tool  prints", events[0][1])
            self.assertIn("tool says hello on stdout", events[1][1])
            self.assertIn("tool says hello on stderr", events[2][1])
            self.assertIn("printed hi", events[3][1])
            self.assertIn("turn 1 prints", events[4][1])

    def test_a_tool_sees_the_writer_while_it_runs_and_the_channels_after(self):
        seen = []

        def peek(name: str) -> str:
            seen.append(sys.stdout)
            return "ok"

        saved = sys.stdout
        with quiet_run(io.StringIO()) as (args, writer, meter, out, err):
            registry = {"peek": (peek, JERRY_MOD.tool_spec(peek), "test")}
            JERRY_MOD.call_tool(args, writer, meter, registry, "peek", '{"name": "x"}', 1)
        self.assertIsInstance(seen[0], JERRY_MOD.Proxy)
        self.assertIs(seen[0].writer, writer)
        self.assertIs(sys.stdout, saved, "the channel is put back after the call")

    def test_the_tool_log_line_is_the_one_in_tools_md(self):
        """timestamp, turn, tool, arguments, result, how long it took."""
        with quiet_run(io.StringIO()) as (args, writer, meter, out, err):
            JERRY_MOD.report_call(args, writer, 1, "get_weather", '{"b": 2, "a": 1}', "Sunny, 21C", time.monotonic())
            self.assertRegex(writer.log_fh.getvalue().strip(),
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} turn 1 get_weather "
                             r"\{\"a\": 1, \"b\": 2\} -> Sunny, 21C \(\d+\.\d{2}s\)$")

    def test_the_arguments_in_the_log_are_written_sorted(self):
        with quiet_run(io.StringIO()) as (args, writer, meter, out, err):
            JERRY_MOD.report_call(args, writer, 1, "get_weather", '{"b": 2, "a": 1}', "Sunny", time.monotonic())
            self.assertIn('{"a": 1, "b": 2}', writer.log_fh.getvalue())

    def test_the_log_is_written_whatever_v_is_doing(self):
        """No -v, and -q on top of it: nothing on stderr, the log still gets its line."""
        log = io.StringIO()
        with quiet_run(log, verbose=False) as (args, writer, meter, out, err):
            args.quiet = True
            JERRY_MOD.call_tool(args, writer, meter, registry_of(TOOLMOD), "get_weather", '{"city": "Paris"}', 1)
            self.assertEqual(err.getvalue(), "")
            self.assertIn("turn 1 get_weather", log.getvalue())

    def test_nothing_is_logged_when_no_log_was_asked_for(self):
        args = JERRY_MOD.parse_args([])
        self.assertIsNone(args.log)
        with quiet_run(None, verbose=False) as (args, writer, meter, out, err):
            JERRY_MOD.call_tool(args, writer, meter, registry_of(TOOLMOD), "get_weather", "{}", 1)
            self.assertIsNone(writer.log_fh)
            self.assertEqual(err.getvalue(), "")

    def test_jerry_tool_log_is_the_default_for_the_log(self):
        self.assertEqual(load_jerry({"JERRY_TOOL_LOG": "/tmp/toolcall.log"}).parse_args([]).log, "/tmp/toolcall.log")

    def test_the_documented_default_number_of_turns(self):
        self.assertEqual(JERRY_MOD.parse_args([]).max_turns, 8)
        self.assertEqual(load_jerry({"JERRY_MAX_TURNS": "3"}).parse_args([]).max_turns, 3)

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# live tests (against the mock server, or a real one with --server)
# --------------------------------------------------------------------------- #

PROMPT = "say hi in three words"
LONG = "write a short story about a key, three sentences"


class TestLive(LiveCase):

    def test_the_default_ports_are_tried_in_that_order(self):
        """llama.cpp moved to 9931, 8080 stays as the fallback of the default."""
        self.assertEqual(JERRY_MOD.url_candidates(), ["http://localhost:9931", "http://localhost:8080"])
        args = JERRY_MOD.parse_args([])
        args.url_candidates = ["http://127.0.0.1:9/", SERVER]      # nothing on the first port
        args.url = args.url_candidates[0]
        JERRY_MOD.open_server(args, "/v1/models").read()
        self.assertEqual(args.url, SERVER, "a port that answers nothing should be passed over")

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


# --------------------------------------------------------------------------- #
# tom and jerry together, against a server
# --------------------------------------------------------------------------- #

class TestMultimodalLive(LiveCase):

    def test_the_pipeline_works_end_to_end(self):
        t, j = tom_then_jerry(["-", "tests/red.png"], stdin="what is in this image?")
        self.assertEqual(t.returncode, EXIT_OK, t.stderr.decode())
        self.assertEqual(j.returncode, EXIT_OK, j.stderr.decode())
        self.assertTrue(j.stdout.strip(), "no response on stdout")
        self.assertEqual(j.stderr, b"", "stderr must stay empty by default")
        self.assertTrue(j.stdout.endswith(b"\n"))

    def test_the_server_sees_the_content_array_not_the_tags(self):
        """The mock only writes an <image> marker for a content array entry, so an answer
        that contains one proves the tags were taken apart before the request."""
        t, j = tom_then_jerry(["-", "tests/red.png"], stdin="what is in this image?")
        answer = j.stdout.decode("utf-8", "replace")
        self.assertNotIn("<jerry:", answer, "the tag ids must not reach the server")
        if SERVER != MOCK_URL:
            self.skipTest("the <image> marker is the mock's, so this half only runs here")
        self.assertIn("<image>", answer, "the content array did not arrive")
        self.assertIn("what is in this image?", answer)

    def test_attachments_arrive_in_declaration_order(self):
        if SERVER != MOCK_URL:
            self.skipTest("the proof is the mock echoing the prompt back; a real model does not")
        md = os.path.join(ROOT, "tests", ".tmp_order.md")
        with open(md, "w") as fh:
            fh.write("middle note")
        try:
            _, j = tom_then_jerry(["tests/red.png", md, "tests/blue.jpg"])
            self.assertEqual(j.returncode, EXIT_OK, j.stderr.decode())
            self.assertRegex(j.stdout.decode("utf-8", "replace"),
                             r"mock reply for: <image>\s+middle note\s+<image>",
                             "the order of the entries is the order tom was given")
        finally:
            os.unlink(md)

    def test_the_same_files_get_the_same_answer_run_after_run(self):
        """Random tag ids, identical requests: what the server sees does not depend on them."""
        a = tom_then_jerry(["-", "tests/red.png"], stdin="what is in this image?")
        b = tom_then_jerry(["-", "tests/red.png"], stdin="what is in this image?")
        self.assertNotEqual(a[0].stdout, b[0].stdout, "tom picked a different id, as it should")
        self.assertEqual(json.dumps(JERRY_MOD.parse_multimodal(a[0].stdout.decode())),
                         json.dumps(JERRY_MOD.parse_multimodal(b[0].stdout.decode())),
                         "the request is the same whatever ids tom picked")
        if a[1].stdout != b[1].stdout:
            self.skipTest("this server does not honour the seed (documented in the README)")
        self.assertEqual(a[1].stdout, b[1].stdout)

    def test_input_without_tags_is_sent_as_plain_string_content(self):
        """Input with no matched tag pair is sent as it came in, unchanged."""
        j = subprocess.run([JERRY], input=b"just text, no tags", capture_output=True,
                           cwd=ROOT, timeout=TIMEOUT, env=dict(os.environ, JERRY_URL=SERVER))
        self.assertEqual(j.returncode, EXIT_OK, j.stderr.decode())
        if SERVER != MOCK_URL:
            self.skipTest("the expected answer is the mock's canned one")
        self.assertEqual(j.stdout.decode("utf-8", "replace").strip(),
                         "mock reply for: just text, no tags")

    def test_e_echoes_what_it_got_and_the_round_trip_rebuilds_the_same_content(self):
        """-e prints the input as it came in, tags intact, so a tom answer can be fed back
        into jerry and get a little more output."""
        t, first = tom_then_jerry(["tests/red.png", "-"], ["-e"], stdin="what is in this image?")
        echo = first.stdout.decode("utf-8", "replace")
        sent = t.stdout.decode("utf-8", "replace")
        self.assertEqual(echo[:len(sent)], sent, "-e must echo the input verbatim, tags and all")
        self.assertEqual(tom_ids(echo), tom_ids(sent), "the ids must be the ones tom picked")

        entries = JERRY_MOD.parse_multimodal(echo)
        self.assertEqual(entries[0], {"type": "image_url", "image_url": {"url": tom_payloads(sent)[0]}},
                         "the attachment must survive the round trip")
        answer = echo[len(sent):].strip()
        self.assertIn("what is in this image?", entries[1]["text"])
        self.assertIn(answer, entries[1]["text"], "the first answer is now part of the input")

        second = subprocess.run([JERRY], input=first.stdout, capture_output=True, cwd=ROOT,
                                timeout=TIMEOUT, env=dict(os.environ, JERRY_URL=SERVER))
        self.assertEqual(second.returncode, EXIT_OK, second.stderr.decode())
        self.assertTrue(second.stdout.strip(), "the round trip produced nothing")
        if SERVER == MOCK_URL:
            self.assertIn(answer, second.stdout.decode("utf-8", "replace"),
                          "the mock answers from the text it was sent, which is now the chain")

    def test_an_image_with_no_text_at_all_still_reports_prompt_tokens(self):
        """Nothing but an attachment: the summary line needs prompt_tokens to be more than
        zero, so the mock charges a vision slot for the image."""
        _, j = tom_then_jerry(["tests/red.png"], ["-v"])
        self.assertEqual(j.returncode, EXIT_OK, j.stderr.decode())
        reported = re.search(r"jerry: prompt  (\d+) tokens", j.stderr.decode("utf-8", "replace"))
        self.assertTrue(reported, "the summary line needs prompt_tokens, so an image must count for something")
        if SERVER == MOCK_URL:
            self.assertGreater(int(reported.group(1)), mock_server.VISION_TOKENS,
                               "the mock did not count the image in the prompt")


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# tools, against a server
# --------------------------------------------------------------------------- #

class TestToolCallingLive(LiveCase):

    @staticmethod
    def tool_run(args, prompt, **kwargs):
        return Run(list(args) + ["-T", TOOLMOD, prompt], stdin=prompt, **kwargs)

    def test_a_tool_call_is_run_and_the_result_is_fed_back(self):
        prompt = "call the tool get_weather"
        r = self.tool_run([], prompt).assert_ok()
        if SERVER != MOCK_URL:
            self.skipTest("the proof is the mock echoing the tool result back")
        self.assertEqual(r.err, b"", "stderr is nothing by default")
        self.assertEqual(r.out.decode().strip(), "tool said: mock weather in Paris (c)",
                         "the answer the model gave after the tool ran")

    def test_a_tool_the_model_does_not_call_changes_nothing(self):
        r = self.tool_run([], PROMPT).assert_ok()
        if SERVER != MOCK_URL:
            self.skipTest("the expected answer is the mock's canned one")
        self.assertEqual(r.out.decode().strip(), "mock reply for: " + PROMPT)
        self.assertEqual(r.err, b"")

    def test_tool_interaction_is_only_printed_with_v(self):
        prompt = "call the tool get_weather"
        quiet = self.tool_run([], prompt).assert_ok()
        loud = self.tool_run(["-v"], prompt).assert_ok()
        if SERVER != MOCK_URL:
            self.skipTest("the run needs the mock, which only answers with a tool call")
        self.assertNotIn("jerry: tool", quiet.err.decode(), "nothing about a call without -v")
        self.assertIn("jerry: tool  get_weather({\"city\": \"Paris\"})", loud.err.decode())
        self.assertIn("jerry: tool  get_weather -> mock weather in Paris (c)", loud.err.decode())
        self.assertNotIn("jerry: tool", loud.out.decode(), "the call is not the response")

    def test_the_tools_line_lists_the_tools(self):
        text = self.tool_run(["-v"], PROMPT).assert_ok().err.decode()
        lines = [l for l in text.splitlines() if l]
        self.assertEqual(lines[0], "jerry: server  %s" % SERVER)
        self.assertIn("jerry: tools   anything, broken, get_weather, list_cities, note, prints, silent, "
                      "slow, untyped", lines)

    def test_a_tool_failure_is_fed_back_to_the_model_and_the_run_carries_on(self):
        prompt = "call the tool broken city fail"
        r = self.tool_run([], prompt).assert_ok()
        if SERVER != MOCK_URL:
            self.skipTest("the proof is the mock echoing the tool result back")
        self.assertIn("the weather service is down", r.out.decode())
        self.assertEqual(r.err, b"", "a tool that fails is not jerry's problem")

    def test_a_tool_name_the_module_does_not_have_is_fed_back_as_well(self):
        prompt = "call the tool translate"
        r = self.tool_run([], prompt).assert_ok()
        if SERVER != MOCK_URL:
            self.skipTest("the proof is the mock echoing the tool result back")
        self.assertEqual(r.out.decode().strip(), "tool said: no tool named translate")

    def test_the_meter_shows_the_tool_phase(self):
        prompt = "call the tool get_weather"
        text = self.tool_run(["-v"], prompt).assert_ok().err.decode()
        self.assertRegex(text, r"jerry: \d+/\d+ +\d+\.\d+% +\d+\.\d+ t/s +\d+\.\d+s tool")

    def test_tool_output_keeps_its_order(self):
        """The call line, whatever the tool printed while it ran, then the result line."""
        prompt = "call the tool prints note hello"
        r = self.tool_run(["-v"], prompt).assert_ok()
        call = r.first("err", r"jerry: tool  prints")
        printed = r.first("out", r"tool says hello on stdout")
        echoed = r.first("err", r"tool says hello on stderr")
        result = r.first("err", r"jerry: tool  prints -> printed hello")
        for stamp, name in ((call, "the call line"), (printed, "the tool's stdout line"),
                            (echoed, "the tool's stderr line"), (result, "the result line")):
            self.assertIsNotNone(stamp, "%s never arrived" % name)
        self.assertLess(call, printed, "the tool printed before jerry had said what it called")
        self.assertLess(printed, echoed, "the tool's own lines came out of order")
        self.assertLess(echoed, result, "the result line came before the tool finished")

    def test_on_a_terminal_the_meter_still_owns_stderr_and_the_order_keeps(self):
        """Nothing is mixed into the meter's line: what it recorded is written after the run,
        in the order it happened."""
        prompt = "call the tool prints note hello"
        r = self.tool_run(["-v"], prompt, tty_stderr=True).assert_ok()
        text = r.err.decode("utf-8", "replace")

        def pos(pattern):
            match = re.search(pattern, text)
            self.assertIsNotNone(match, "%s never arrived" % pattern)
            return match.start()

        meter = pos(r"jerry: \d+/\d+ .* tool")          # the meter's own line, live
        call, printed, result = (pos(r"jerry: tool  prints"), pos(r"tool says hello on stderr"),
                                 pos(r"jerry: tool  prints -> printed hello"))
        self.assertLess(meter, call, "the meter keeps its line while a tool runs")
        self.assertLess(call, printed, "the tool's own line came before the tool spoke")
        self.assertLess(printed, result, "the tool's own line came after its result")
        stdout = r.first("out", r"tool says hello on stdout")
        self.assertIsNotNone(stdout, "the tool's stdout line never arrived")
        self.assertGreater(stdout, r.first("err", r"jerry: \d+/\d+ .* tool"),
                           "written once the run is over, in the order it happened")

    def test_the_tool_log_is_written_in_the_documented_format(self):
        path = os.path.join(HERE, ".tmp_toollog.txt")
        prompt = "call the tool get_weather"
        self.tool_run(["-L", path], prompt).assert_ok()
        with open(path) as fh:
            line = fh.read().strip()
        self.assertRegex(line,
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} turn 1 get_weather "
                         r"\{\"city\": \"Paris\"\} -> mock weather in Paris \(c\) \(\d+\.\d{2}s\)$")
        os.unlink(path)

    def test_the_tool_log_is_appended_and_not_rewritten(self):
        path = os.path.join(HERE, ".tmp_toollog.txt")
        prompt = "call the tool get_weather"
        with open(path, "w") as fh:
            fh.write("an earlier line\n")
        try:
            for _ in range(2):
                self.tool_run(["-L", path], prompt).assert_ok()
        finally:
            with open(path) as fh:
                lines = fh.read().splitlines()
            os.unlink(path)
        self.assertEqual(lines[0], "an earlier line", "an existing log is appended to, not replaced")
        self.assertEqual(len(lines), 3, "one line per call")
        self.assertIn("turn 1 get_weather", lines[1])

    def test_the_tool_log_records_a_failure_as_well(self):
        path = os.path.join(HERE, ".tmp_toollog.txt")
        prompt = "call the tool broken city fail"
        self.tool_run(["-L", path], prompt).assert_ok()
        with open(path) as fh:
            line = fh.read()
        os.unlink(path)
        self.assertIn("turn 1 broken {\"city\": \"fail\"} -> tool broken failed", line)

    def test_an_unwritable_tool_log_is_a_usage_error(self):
        prompt = "call the tool get_weather"
        r = Run(["-L", "/definitely-not-a-directory/tool.log", "-T", TOOLMOD, prompt], stdin=prompt)
        self.assertEqual(r.code, EXIT_USAGE)
        self.assertIn("cannot write the tool log", r.err.decode())

    def test_the_turn_limit_stops_a_model_that_keeps_calling_tools(self):
        prompt = "call the tool again and again"
        r = Run(["--max-turns", "2", "-T", TOOLMOD, prompt], stdin=prompt)
        if SERVER != MOCK_URL:
            self.skipTest("only the mock calls a tool every turn")
        self.assertEqual(r.code, EXIT_TOOL)
        self.assertIn("kept asking for tools after 2 turns", r.err.decode())

    def test_the_documented_turn_limit_is_the_default(self):
        prompt = "call the tool again and again"
        r = Run(["-T", TOOLMOD, prompt], stdin=prompt)
        if SERVER != MOCK_URL:
            self.skipTest("only the mock calls a tool every turn")
        self.assertEqual(r.code, EXIT_TOOL)
        self.assertIn("after 8 turns", r.err.decode())

    def test_two_modules_that_define_the_same_tool_exit_one(self):
        path = os.path.join(HERE, ".tmp_same_tool.py")
        with open(path, "w") as fh:
            fh.write("def tool_get_weather(x: str) -> str:\n    return x\n")
        try:
            r = Run(["-T", TOOLMOD, "-T", path, PROMPT], stdin=PROMPT)
            self.assertEqual(r.code, EXIT_USAGE)
            self.assertIn("both define tool get_weather", r.err.decode())
        finally:
            os.unlink(path)

    def test_a_tool_module_with_no_tools_exits_one(self):
        path = os.path.join(HERE, ".tmp_no_tools.py")
        with open(path, "w") as fh:
            fh.write("def nothing():\n    return 1\n")
        try:
            r = Run(["-T", path, PROMPT], stdin=PROMPT)
            self.assertEqual(r.code, EXIT_USAGE)
            self.assertIn("exports no tool_", r.err.decode())
        finally:
            os.unlink(path)

    def test_interrupting_while_a_tool_runs_exits_130(self):
        prompt = "call the tool slow note stop"
        proc = subprocess.Popen([JERRY, "-v", "-T", TOOLMOD, prompt], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT,
                                env=dict(os.environ, JERRY_URL=SERVER))
        proc.stdin.write((prompt + "\n").encode())
        proc.stdin.close()
        # interrupt while the tool is running, not while the model is
        deadline = time.monotonic() + TIMEOUT
        started = False
        for raw in proc.stderr:
            if re.search(r"jerry: \d+/\d+ .* tool", raw.decode("utf-8", "replace")):
                started = True
                break
            if time.monotonic() > deadline:
                break
        if not started:
            proc.kill()
            self.fail("no meter update for the tool phase, nothing to interrupt")
        proc.send_signal(signal.SIGINT)
        err = proc.stderr.read()
        code = proc.wait(timeout=60)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(code, EXIT_KILLED)
        self.assertIn("interrupted", err.decode("utf-8", "replace"))

    def test_the_output_limit_counts_a_turn_and_not_the_whole_run(self):
        """Five turns of one token each are under a limit of 3, so the run ends on the
        turn limit, not on the output limit."""
        prompt = "call the tool again and list the numbers 1 to 4"
        r = Run(["-f", "--limit", "3", "--max-turns", "5", "-T", TOOLMOD, prompt], stdin=prompt)
        if SERVER != MOCK_URL:
            self.skipTest("only the mock calls a tool every turn")
        self.assertEqual(r.code, EXIT_TOOL)
        self.assertIn("kept asking for tools after 5 turns", r.err.decode())
        self.assertNotIn("output limit reached", r.err.decode())

    def test_the_session_is_the_first_message_then_the_call_and_its_answer(self):
        if SERVER != MOCK_URL:
            self.skipTest("the proof is the mock listing the messages it was sent")
        prompt = "call the tool get_weather and list the messages"
        r = self.tool_run([], prompt).assert_ok()
        text = r.out.decode()
        self.assertIn("message 0: user", text, "the session starts with the text it was given")
        self.assertIn("message 1: assistant", text, "the model's own call is kept as a message")
        self.assertIn("message 2: tool", text, "what the tool answered is sent back as a message")
        self.assertNotIn("message 3:", text, "one call and one answer, then the model answered")

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
