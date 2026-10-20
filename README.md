* jerry — *To (Large Language) Module*

`jerry` is a simple client for piping input to a llama.cpp server. It runs the text you give it through a fresh session, waits for the model to finish, and prints the raw response on stdout.

```sh
cat instructions.txt | jerry                  # fresh session, raw response on stdout
echo hello! tell me a story | jerry           # same, "hello!" is what is sent
```

## `tom` — multimodal attachments

`tom` is a companion CLI that lets you send images (or any binary) alongside text to multimodal servers. It encodes binaries as base64 and wraps each payload in a `<jerry:HEXD>` tag; `jerry` recognises those tags and builds a proper `content` array.

```sh
echo "what's in this image?" | tom - photo.png           # text + image
tom intro.txt - photo.png                               # text file, stdin, image
./tom photo.png prompt.md | jerry                    # image first, then text
```

`tom` reads files in declaration order. Binary files (anything not `*.txt` or `*.md`) are base64-encoded and wrapped; text files are passed verbatim inside tags. A `-` argument passes stdin through at that position without wrapping. Each attachment gets a unique tag ID.

`jerry` stays backwards-compatible: plain text input without tags works exactly as before.

## Options

| flag | meaning |
| --- | --- |
| `-v`, `--verbose` | diagnostics to stderr, including a progress meter (tokens produced, tokens/s, elapsed, phase) |
| `-r`, `--reason` | print the model's reasoning to stderr |
| `-f`, `--fast` | disable reasoning |
| `-b`, `--big` | no output token limit at all (default limit is 32768 tokens) |
| `-e`, `--echo` | do not clip the input from the output: print input and response as one |
| `-s`, `--seed` `[SEED]` | seed; fixed by default, no argument picks a random one |
| `-i`, `--input FILE` | read the text from a file instead of stdin (`-` means stdin) |
| `-o`, `--output FILE` | write the response to a file instead of stdout |
| `-S`, `--system` | send the text as a system message instead of a user message |
| `-u`, `--url URL` | server url, default `http://localhost:8080` |
| `-m`, `--model NAME` | model name to send |
| `-t`, `--timeout SECONDS` | socket timeout, default 600 |
| `--limit N` | explicit output token limit (overrides the default) |
| `--no-stream` | do not stream the response (no live meter) |
| `-q`, `--quiet` | suppress error messages too |

Input sources, in order of precedence: `-i FILE`, positional argument, stdin.

## What goes where

* **stdout** — the raw response only. It is streamed as it arrives unless `-v` is on and stderr is a terminal, in which case the meter owns stderr and the answer is written once the run is over. `jerry` writes output with a trailing newline.
* **stderr** — nothing by default. Errors are always reported there (and the output-limit message is required by the spec), unless `-q`. With `-v` the meter and diagnostics go there; with `-r` the reasoning goes there.

If the response runs past the token limit.*jerry` closes with an error message on stderr and exit code 3.

```sh
jerry: output limit reached (32768 tokens), the response is incomplete; use -b for no limit or --limit N
```

### A `-v` run

```
jerry: server  http://localhost:8080
jerry: model   qwen3.8-flash-next-iq2_xs
jerry: seed    0
jerry: limit   32768 tokens
jerry: reasoning hidden
jerry: input   42 chars as user message
jerry: 63/32768   0.2%  18.5 t/s   4.9s streaming
The old key still opened the door ...
jerry: finish  stop
jerry: prompt  63 tokens (36.4 t/s)
jerry: output  94 tokens (29.7 t/s) first token 1.79s
jerry: reasoning 268 chars, use -r to see it
```

## Exit codes

`0` ok, `1` usage error, `2` server/connection error, `3` output limit reached, `130` interrupted.

## Determinism

`jerry` sends `seed` on every request (default `0`, `JERRY_SEED` to change the default, `-s N` per run, `-s` alone for a random one).

Two things `jerry` cannot control:

* not every server honours the seed. The patched server on `localhost:8080` used for testing does not — five identical requests with the same seed gave five different answers. Check your own server by running the same input twice:

  ```sh
  echo "tell me a story about a key" | jerry
  echo "tell me a story about a key" | jerry
  ```

* batching / several slots make a server non-deterministic even with a seed.

There is no client side response cache: the only cache in play is the server's own prompt/KV cache, so switching the model on the server always gives fresh answers rather than stale ones.

## Notes / limits

* `-f` is best effort: the OpenAI protocol has no portable way to switch thinking off, so `jerry` sends `enable_thinking: false` (what llama.cpp chat templates use) and ignores it if the server does not. Reasoning is never in stdout either way, so `-f` mainly saves time on servers that support it.
* `jerry` has no tools to run. If the model requests a tool call, `jerry` prints the call, reports it on stderr and stops with exit code 2.
* Only the standard llama.cpp endpoints are used: `POST /v1/chat/completions` (streaming SSE when possible, plain JSON otherwise) and `GET /v1/models` for the diagnostics line. Stock python3, standard library only.
