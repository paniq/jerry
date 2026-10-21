# tests

`test_jerry.py` checks `jerry` and `tom` against the README spec. By default it starts the
bundled mock server (`mock_server.py`) on a free port, so no model is needed and a full run
takes about 25 seconds.

```sh
python3 tests/test_jerry.py                        # bundled mock server
python3 tests/test_jerry.py --server http://host:port   # a real server (or JERRY_TEST_URL)
python3 tests/test_jerry.py -k broken_pipe         # subset; -k can be repeated, and it
                                                   # matches the test method name (test_tag_pair, ...)
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

Message content is taken as either a string or a multimodal content array. An array is
flattened to text with one `<image>` marker per image entry, which is what lets a test
see that the array arrived rather than the raw `<jerry:...>` tags, and `prompt_tokens`
charges a vision slot (`VISION_TOKENS`, a stand-in, not a real count) per image so an
image-only run still reports prompt tokens.

Because the mock always produces reasoning and is deterministic, the tests that
skip themselves against a real model (reasoning, seeded repeat) all run here, and the tool
call test only runs against the mock (a real server never gets tools from `jerry`).

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

`tom` (no server needed): matched `<jerry:HEXD>` pairs, `.txt`/`.md` passed verbatim,
everything else base64 with the right MIME, stdin passed through untagged at its position
and byte for byte, files in declaration order, one stdin read only, usage errors exit 1,
broken pipe exits 0 silently, `-q`, `--help`, `--version`, and the flags documented in the
README table are in `--help`.

Multimodal: `data:` payloads become `image_url` entries and anything else `text`, text
between tags keeps its own entry, payloads spanning lines are still content (a tag is only
content when the pair matches), an unmatched pair is sent as the text it is, order is kept,
`-S` still decides the role, the payload gains nothing else, the content array carries only
the payloads so the same files send the same request run to run even though `tom` picks new
IDs each time. `-e` echoes the input as it received it, tags intact, so feeding it back rebuilds
the same content with the previous answer now part of the text.

## Observed result

104 tests against the mock, all pass, none skipped (about 25 seconds).

`jerry` bugs, both fixed since:

1. usage errors exited 2 (argparse) instead of 1
2. broken pipe printed `Exception ignored ... BrokenPipeError` and exited 120

One more, found while writing the multimodal tests:

3. the tag regex had no `re.DOTALL`, so a `.txt`/`.md` payload containing a newline was
   never recognised as content and the raw tags were sent to the server

What still differs on a real server: the determinism test skips if the server ignores the
seed (as the README documents for this one), and some tests skip when the model produces no
reasoning or spends the whole limit on it.
