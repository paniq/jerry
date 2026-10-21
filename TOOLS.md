# Tools

`jerry` has no built-in tools. Instead, it loads them from Python modules you provide:

```sh
echo "what is the weather in Paris?" | jerry -T tools/weather.py -v
```

Every top-level function in the module whose name starts with `tool_` becomes a tool. Everything else is left alone, so a tool module can freely define helper functions.

```python
def tool_get_weather(city: str, units: str = "c") -> str:
    """Current weather for a city.

    Args:
        city: city name
        units: 'c' or 'f'
    """
    ...

def tool_list_cities() -> list[str]:
    """Cities known to the server."""
    ...
```

No additional metadata is needed — `jerry` reads everything from the function signature and docstring.

## Parameter Mapping

| Source               | Tool metadata produced  |
| -------------------- | ----------------------- |
| Function name        | Tool name (`tool_` prefix removed) |
| First docstring block | Tool description        |
| `Args:` section      | Per-parameter descriptions |
| Type annotations     | JSON Schema type         |
| No default value     | Parameter is `required` |
| Default value        | Parameter has `default`; not required |

`*args` and `**kwargs` are not sent to the model.

### Supported Annotations

| Python type             | JSON Schema type      |
| ----------------------- | --------------------- |
| `str`                   | `string`              |
| `int`                   | `integer`             |
| `float`                 | `number`              |
| `bool`                  | `boolean`             |
| `list[X]` / `tuple[X, ...]` | `array` of X      |
| `dict[str, X]`          | `object`              |
| `Optional[X]` / `X \| None` | `anyOf` (X, null) |
| `Literal[...]`          | `enum`                |

If `jerry` cannot map an annotation (or none is given), the parameter receives a description but no type. String annotations (`from __future__ import annotations`) are resolved when possible.

```sh
jerry -T tools/weather.py -T tools/cities.py   # multiple modules are supported
```

## Error Handling

The following situations cause `jerry` to exit with code 1:

- Two modules define a tool with the same name.
- A module contains no `tool_` functions.
- A module cannot be read or raises an exception during import.

## Options

| Flag                    | Meaning                                         |
| ----------------------- | ------------------------------------------------ |
| `-T`, `--tool` MODULE  | Load tools from this module (repeatable)           |
| `-L`, `--log` FILE     | Append timestamped tool calls to FILE (`JERRY_TOOL_LOG`) |
| `--max-turns` N        | Max tool-request turns per run (default 8, `JERRY_MAX_TURNS`) |

A *turn* is one request to the model plus any tools it calls. If the model exceeds `--max-turns`, `jerry` stops with exit code 4.

## Execution

The model sends a tool call as a name and JSON arguments. `jerry` calls the corresponding function with those arguments as keyword parameters. The return value is fed back to the model in the same session:

- **`str`** — returned as-is.
- **Anything else** — serialized to JSON.
- **`None`** (no return) — returned as the string `"no result"`.

Errors are never fatal. If a tool raises, the arguments are invalid JSON, or the model calls a non-existent tool, `jerry` reports the error as the tool's response so the model can react. The run continues with exit code 0:

```
tool broken failed: ValueError: the weather service is down
no tool named translate
the arguments are not valid json: {city":
```

## Output

`jerry`'s own lines about tool calls go to **stderr** and appear only with `-v`:

```
jerry: tools   get_weather, list_cities
jerry: tool  get_weather({"city": "Paris"})
jerry: tool  get_weather -> Sunny, 21C (0.03s)
```

A tool function may print to `stdout` or `stderr` freely. `jerry` ensures that output appears in the correct order (call line → tool output → result line) via its ordered writer. `stdout` remains the response stream, so on a terminal where a meter owns stderr, the tool's output is flushed only after the run completes. When `-o FILE` is given, tool `stdout` is written to that file as well. The `-q` flag silences `jerry` but never a tool.

## Tool Log

`-L FILE` appends one line per call to *FILE*, regardless of `-v`. This file is intended for post-run analysis, so the format is fixed:

```
2026-10-21T18:24:52+02:00 turn 1 get_weather {"city": "Paris"} -> mock weather in Paris (c) (0.00s)
2026-10-21T18:24:53+02:00 turn 2 broken {"city": "fail"} -> tool broken failed: ValueError: the weather service is down (0.00s)
```

Each line contains:

1. Timestamp (local, with offset)
2. Turn number
3. Tool name
4. Arguments as a single sorted JSON object
5. Result or failure message
6. Duration in seconds

Arguments are sorted so that identical calls produce identical log lines.

## Determinism

Tools are sent to the model in name order, so the same set of modules produces the same request regardless of the order `-T` flags are given. `jerry` executes a tool only when the model asks for it, so a run that involves tools is only as reproducible as the tools themselves — any tool that reads a file, the clock, or the network is inherently non-deterministic.
