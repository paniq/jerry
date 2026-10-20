# jerry — a pipe to llama.cpp

`jerry` is a single-shot, pipe-friendly Python client for a [llama.cpp](https://github.com/ggerml llama.cpp) server. It sends your input through a fresh session, waits for the model to finish, and prints the raw response on **stdout**.

```sh
cat instructions.txt | jerry                  # fresh session, raw response on stdout
echo "hello! tell me a story" | jerry           # the piped text is the prompt
jerry "tell me a story"                         # same, positional argument
```

`tom` is a companion CLI for sending images (or any binary) alongside text to multimodal servers. See below.

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
jerry -S "You are a helpful assistant."   # system message
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
- A `-` argument passes stdin through at that position without wrapping.
- Each attachment gets a unique random tag ID.

`jerry` remains backwards-compatible: plain text input without tags works exactly as before.

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
| `-t`, `--timeout` S | Socket timeout in seconds, default 600 |
| `--limit` N | Explicit output token limit (overrides the default) |
| `--no-stream` | Do not stream the response (no live meter) |
| `-q`, `--quiet` | Suppress error messages too |
| `--version` | Print version and exit |

**Input sources**, in order of precedence: `-i FILE` → positional argument → stdin.

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

There is no client-side response cache: the only cache in play is the server's own prompt/KV cache, so switching the model on the server always gives fresh answers.

---

## Notes & limits

- **`-f` is best effort.** The OpenAI protocol has no portable way to switch thinking off, so `jerry` sends `enable_thinking: false` (what llama.cpp chat templates use) and ignores it if the server does not. Reasoning is never in stdout either way, so `-f` mainly saves time on servers that support it.
- **No tool support.** If the model requests a tool call, `jerry` prints the call, reports it on stderr, and stops with exit code 2.
- **Only standard llama.cpp endpoints are used:** `POST /v1/chat/completions` (streaming SSE when possible, plain JSON otherwise) and `GET /v1/models` for the diagnostics line.
- **Stock Python 3, standard library only.** No third-party dependencies.

---

## Testing

```sh
python3 tests/test_jerry.py                  # bundled mock server
python3 tests/test_jerry.py --server URL     # test against a real server
python3 tests/test_jerry.py -k pipe          # run a subset
```

---

## License

MIT — see [LICENSE](LICENSE)
