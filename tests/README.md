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

For tool runs: `call the tool [name]` is a call for that tool, `call the tool again` is a
call every turn (the turn limit test), a turn that already carries a tool result answers
with `tool said: <what jerry fed back>`, and a prompt that asks for the messages gets back
the roles it was sent. The arguments of a call are built from the tool's own declaration,
so a tool is never handed keywords it does not take.

Message content is taken as either a string or a multimodal content array. An array is
flattened to text with one `<image>` marker per image entry, which is what lets a test
see that the array arrived rather than the raw `<jerry:...>` tags, and `prompt_tokens`
charges a vision slot (`VISION_TOKENS`, a stand-in, not a real count) per image so an
image-only run still reports prompt tokens.

Because the mock always produces reasoning and is deterministic, the tests that
skip themselves against a real model (reasoning, seeded repeat) all run here. The tool
tests need the mock as well: a real model does not have to call a tool, so they skip
themselves with `--server`.

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
* default ports tried in order, 9931 then 8080, with the first one that answers kept, and
  no fallback for a url given on the command line or in the environment
* request payload: seed on every request, default limit 32768, no temperature /
  top_k / vendor settings, one message only, `/v1/chat/completions` + `/v1/models`
  only, standard library only, no client side cache, tools only sent when a module
  was given and only in name order
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

Tools (`toolmod.py` is the fixture module, so the metadata is read off a real declaration):
the tool_ functions are the tools and nothing else is, the name is the function name without
the prefix, the description is the first block of the docstring, the `Args:` block gives each
parameter its description, annotations become JSON Schema types (array item type kept), a
default is reported and makes the parameter optional, `*args` is not a parameter, a name that
only reexports a tool counts, and a module with no tool, that cannot be read, that raises
while loading, or that defines the same tool as another module is a usage error. What a tool
returns: text as it is, anything else as JSON, nothing as `no result`; a failure, bad
arguments and a name the module does not have are fed back to the model instead of ending the
run. The one ordered writer: the call line, whatever the tool printed while it ran, the result
line, in that order, on a pipe and on a terminal; a tool sees the writer for the call and the
real channels afterwards. The tool log is the line documented in TOOLS.md (timestamp, turn,
tool, sorted arguments, result, time), it is appended rather than rewritten, and it is written
whatever `-v` or `-q` is doing. Against a server: a call is run and its answer fed back,
nothing is printed about a call without `-v`, the tool phase shows on the meter, an interrupt
while a tool runs exits 130, the turn limit stops a model that keeps calling, and the session
is the first message plus the model's call and the tool's answer, and the output limit counts a turn
rather than the whole run.

## Observed result

157 tests against the mock, all pass, none skipped (about 30 seconds).

`jerry` bugs, both fixed since:

1. usage errors exited 2 (argparse) instead of 1
2. broken pipe printed `Exception ignored ... BrokenPipeError` and exited 120

One more, found while writing the multimodal tests:

3. the tag regex had no `re.DOTALL`, so a `.txt`/`.md` payload containing a newline was
   never recognised as content and the raw tags were sent to the server

Found while writing the tool tests:

4. `Writer.close()` closed the log file and then flushed it (`ValueError: I/O operation on
   closed file`)
5. `merge` summed the usage numbers as if every value were a number, so a server that sends
   a nested object in `usage` or `timings` raised TypeError
6. `Literal[...]` was read as if its values were types, so an enum got no type
7. `take` took the arguments of a tool call as they arrived in one piece, so a call streamed
   in fragments lost everything after the first

What still differs on a real server: the determinism test skips if the server ignores the
seed (as the README documents for this one), and some tests skip when the model produces no
reasoning or spends the whole limit on it.
