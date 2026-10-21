# jerry — a pipe to llama.cpp

`jerry` is a single-shot, pipe-friendly Python client for a [llama.cpp](https://github.com/ggml-org/llama.cpp) server. It sends your input through a fresh session, waits for the model to finish, and prints the raw response on **stdout**.

```sh
cat instructions.txt | jerry                    # fresh session, raw response on stdout
echo "hello! tell me a story" | jerry           # the piped text is the prompt
jerry "tell me a story"                         # same, positional argument
```

`tom` is a companion CLI for sending images (or any binary) alongside text to multimodal servers. See below.

With `-T MODULE.py` `jerry` can run tools: every `tool_` function in that module is offered to
the server, the call is run, and the result is fed back. Writing a tool module is meant to
nothing more than writing a function — see [TOOLS.md](TOOLS.md).

---

## Installation

`jerry` is a standalone script with no dependencies beyond the Python 3 standard library. Make it executable and put it on your `PATH`:

```sh
chmod +x jerry
sudo cp jerry /usr/local/bin/
```

---

## Usage

### Basic

```sh
echo "what's 2+2?" | jerry
jerry -S "You are a helpful assistant."    # system message
jerry -i prompt.txt                        # read prompt from file
jerry -o response.txt "hello"              # write response to file
```

### Multimodal with `tom`

`tom` reads files, base64-encodes binaries, and wraps each payload in a `<jerry:HEXD>` tag. `jerry` recognises those tags and constructs a proper multimodal `content` array.

```sh
# Text + image
echo "what's in this image?" | tom - photo.png | jerry

# Text file + image
tom intro.txt - photo.png | jerry

# Multiple attachments (tom reads in declaration order)
tom photo1.png prompt.md photo2.jpg | jerry
```

**How `tom` works:**

- Files ending in `.txt` or `.md` are passed verbatim inside tags.
- All other files are base64-encoded with a `data:…;base64,…` URI prefix.
- A `-` argument passes stdin through at that position without wrapping, byte for byte.
  Stdin is read at most once, so `-` appears once (and it is refused when nothing is
  piped into `tom`).
- Each attachment gets a unique random tag ID.

**`jerry` reads the tags back:** each pair that carries the same ID on both ends becomes
one content entry — a payload starting with `data:` as `image_url`, anything else as
`text` — and the text between the pairs keeps its own entry. Anything that is not a matched
pair is sent as the text it is.

### tom options

| Flag | Meaning |
| --- | --- |
| `-q`, `--quiet` | Suppress error messages too |
| `-h`, `--help` | Show usage and exit |
| `--version` | Print version and exit |

Input files in order, `-` for stdin, so `tom` takes no options beyond these. Exit codes:
0 ok, 1 usage error, 130 interrupted; a broken pipe exits 0 silently, as `jerry` does.

With `-e` the input is echoed exactly as `jerry` received it, tags intact, which is what
makes the round trip possible: feed `jerry -e` back into `jerry` and the tags are read
again, with the previous answer now part of the text.

---

## Options

| Flag | Meaning |
| --- | --- |
| `-v`, `--verbose` | Print diagnostics and a progress meter to stderr |
| `-r`, `--reason` | Print the model's reasoning to stderr |
| `-f`, `--fast` | Disable reasoning (`enable_thinking: false`) |
| `-b`, `--big` | No output token limit (default: 32 768) |
| `-e`, `--echo` | Print input + response as one (don't clip) |
| `-s`, `--seed` `[N]` | Seed; fixed by default (0), no arg picks random |
| `-i`, `--input` FILE | Read text from a file (`-` means stdin) |
| `-o`, `--output` FILE | Write response to a file instead of stdout |
| `-S`, `--system` | Send text as a system message instead of user |
| `-u`, `--url` URL | Server URL, default `http://localhost:8080` |
| `-m`, `--model` NAME | Model name to send |
| `-t`, `--timeout` S | Socket timeout in seconds, default 600 (`JERRY_TIMEOUT`) |
| `--limit` N | Explicit output token limit (overrides the default) |
| `--no-stream` | Do not stream the response (no live meter) |
| `-T`, `--tool` MODULE.py | Take tools from this module, repeatable (see [TOOLS.md](TOOLS.md)) |
| `-L`, `--log` FILE | Append timestamped tool calls to a file (see [TOOLS.md](TOOLS.md)) |
| `--max-turns` N | How many times the model may ask for tools in one run (default 8, see [TOOLS.md](TOOLS.md)) |
| `-q`, `--quiet` | Suppress error messages too |
| `--version` | Print version and exit |

**Input sources**, in order of precedence: `-i FILE` → positional argument → stdin.

---

## Environment

Every default above can be set from the environment, so a server you use every day only has to be
configured once:

| Variable | Default for | Fallback |
| --- | --- | --- |
| `JERRY_URL` | `-u`, `--url` | `http://localhost:8080` |
| `JERRY_MODEL` | `-m`, `--model` | whatever the server reports |
| `JERRY_SEED` | `-s`, `--seed` | `0` |
| `JERRY_TIMEOUT` | `-t`, `--timeout` | `600` |
| `JERRY_TOOL_LOG` | `-L`, `--log` | no log |
| `JERRY_MAX_TURNS` | `--max-turns` | `8` |

An option given on the command line always wins over the environment.

---

## Output channels

| Channel | Contents |
| --- | --- |
| **stdout** | The raw response only. Streamed as it arrives unless `-v` is on and stderr is a terminal, in which case the meter owns stderr and the full response is written once the run is over. Always ends with a trailing newline. |
| **stderr** | Nothing by default. Errors are always reported there (and the output-limit message is required by the spec), unless `-q`. With `-v` the progress meter and diagnostics go there; with `-r` the reasoning goes there. |

### Output limit

If the response exceeds the token limit, `jerry` exits with code 3 and prints:

```
jerry: output limit reached (32768 tokens), the response is incomplete; use -b for no limit or --limit N
```

---

## Verbose output (`-v`)

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

---

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Usage error |
| 2 | Server / connection error |
| 3 | Output limit reached |
| 4 | Tool turn limit reached (tools in use) |
| 130 | Interrupted (SIGINT) |

---

## Determinism

`jerry` sends `seed` on every request:

- Default: `0`
- Environment variable: `JERRY_SEED`
- Per-run override: `-s N` (use `-s` alone for a random seed)

### Caveats

1. **Not every server honours the seed.** Test your server:

   ```sh
   echo "tell me a story about a key" | jerry -s 0
   echo "tell me a story about a key" | jerry -s 0
   ```

2. **Batching / multiple slots** can make a server non-deterministic even with a seed.

3. **`tom`'s tag IDs are random**, so the text `tom` produces is not byte-identical run to
   run. `jerry` builds the content from the payload, so the same files send the same
   request and a run stays reproducible.

There is no client-side response cache: the only cache in play is the server's own prompt/KV cache, so switching the model on the server always gives fresh answers.

---

## Notes & limits

- **`-f` is best effort.** The OpenAI protocol has no portable way to switch thinking off, so `jerry` sends `enable_thinking: false` (what llama.cpp chat templates use) and ignores it if the server does not. Reasoning is never in stdout either way, so `-f` mainly saves time on servers that support it.
- **Tool support is only what you bring.** With `-T` `jerry` runs the tool functions in the modules given (see [TOOLS.md](TOOLS.md)); a tool that fails is reported to the model rather than ending the run. Without `-T` there is nothing to run: if the model requests a tool call, `jerry` prints the call, reports it on stderr, and stops with exit code 2.
- **Only standard llama.cpp endpoints are used:** `POST /v1/chat/completions` (streaming SSE when possible, plain JSON otherwise) and `GET /v1/models` for the diagnostics line.
- **Stock Python 3, standard library only.** No third-party dependencies.

---

## Testing

See [tests/README.md](tests/README.md).

---

## License

MIT — see [LICENSE](LICENSE)
