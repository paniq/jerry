# tests

`test_jerry.py` checks `jerry` against the README spec. By default it starts the
bundled mock server (`mock_server.py`) on a free port, so no model is needed and
a full run takes about 20 seconds.

```sh
python3 tests/test_jerry.py                        # bundled mock server
python3 tests/test_jerry.py --server http://host:port   # a real server (or JERRY_TEST_URL)
python3 tests/test_jerry.py -k broken_pipe         # subset; -k can be repeated
JERRY_TEST_TIMEOUT=600 python3 tests/test_jerry.py   # per-run deadline (60s mock, 300s real)
```

Stdlib only (unittest), same constraint as `jerry` itself. `JERRY_URL` is ignored on
purpose, so a server configured for daily use is never hit by accident. With
`--server`, tests that need a server are skipped when `/v1/models` does not answer.

## The mock server

`mock_server.py` speaks `GET /v1/models` and `POST /v1/chat/completions`, streaming
(SSE) or plain JSON, with canned deterministic answers chosen by the prompt:
`List the numbers 1 to N`, anything with `story` (a 200 token answer, slow enough
to interrupt), `call the tool` (a tool call), or a short `mock reply for: ...`.
Answers start with 12 reasoning tokens unless `enable_thinking` is false, reasoning
counts towards `max_tokens`, and an answer that does not fit ends with
`finish_reason: length`. `usage` and `timings` are sent so the `-v` summary can be
checked. It can also be run by hand: `MOCK_PORT=8081 python3 tests/mock_server.py`.

Because the mock always produces reasoning and is deterministic, the tests that
skip themselves against a real model (reasoning, seeded repeat) all run here, and
the tool call test only runs against the mock (a real server never gets tools
from `jerry`).

## What is checked

* stdout is the raw response only, ends with a newline, nothing on stderr by default
* exit codes: 0 / 1 usage / 2 server / 3 limit / 130 interrupted
* the limit message is the one printed in the README, `-q` keeps the exit code
* `-v` header and summary lines, meter line format, `-b` meter (`N tokens (no limit)`)
* meter owns stderr on a terminal (answer printed once, after the run); streamed
  live when stderr is a pipe
* `-r` to stderr, reasoning never on stdout, the `use -r to see it` line
* `-e`, `-f`, `-S`, `-s` (fixed / `-s N` / bare `-s` random), `JERRY_SEED`, `-i`, `-o`
* input precedence `-i FILE`, positional, stdin
* `--no-stream`, `-m`, `-u`, `-t`, `--limit`, `-q`, `--version`, `--help` flags
* request payload: seed on every request, default limit 32768, no temperature /
  top_k / vendor settings, one message only, `/v1/chat/completions` + `/v1/models`
  only, standard library only, no client side cache
* broken pipe, SIGINT, command substitution / pipes, determinism (skipped when the
  server does not honour the seed, as the README says it may not)

## Observed result

Mock server: 69 tests, all pass, none skipped (about 20 seconds).).

Against a real model (qwen3.8-flash-next-iq2_xs on localhost:8080, before the
fixes below) 69 passed, none skipped against the mock (about 20 seconds).
`jerry` bugs, both fixed since:

1. usage errors exited 2 (argparse) instead of 1
2. broken pipe printed `Exception ignored ... BrokenPipeError` and exited 120

What still differs on a real server: the determinism test skips if the server
ignores the seed (as the README documents for this one), and some tests skip when
the model produces no reasoning or spends the whole limit on it.
