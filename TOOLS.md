# tools

`jerry` has no tools of its own. It takes them from a python module you give it:

```sh
echo "what is the weather in Paris?" | jerry -T tools/weather.py -v
```

Every function in that module whose name begins with `tool_` is a tool. Everything
else in the module is left alone, so a tool module can have its own helpers.

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

Nothing else has to be written: the metadata sent to the server is read from the
declaration itself.

| where it comes from | what is sent |
| --- | --- |
| the function name | the tool name, `tool_` dropped |
| the first block of the docstring | the tool description |
| the `Args:` block | the description of each parameter |
| the annotation | the JSON Schema type (see below) |
| a parameter with no default | `required` |
| a default value | `default`, and the parameter is not required |

`*args` and `**kwargs` are not parameters and are not sent.

**Annotations.** `str` → string, `int` → integer, `float` → number, `bool` → boolean,
`list[X]`/`tuple[X, ...]` → array of X, `dict[str, X]` → object, `Optional[X]` and `X | None`
→ `anyOf`, `Literal[...]` → `enum`. An annotation `jerry` cannot map, or none at all, leaves
the parameter with a description and no type. String annotations (`from __future__ import
annotations`) are resolved when they can be.

```sh
jerry -T tools/weather.py -T tools/cities.py        # modules can be repeated
```

Two modules that define the same tool name is a usage error (exit code 1), as is a
module with no `tool_` function, a module that cannot be read, or one that raises while
it is loaded.

---

## Options

| Flag | Meaning |
| --- | --- |
| `-T`, `--tool` MODULE.py | take tools from this module, repeatable |
| `-L`, `--log` FILE | append timestamped tool calls to FILE (`JERRY_TOOL_LOG`) |
| `--max-turns` N | how many times the model may ask for tools in one run (default 8, `JERRY_MAX_TURNS`) |

A *turn* is one request to the server plus whatever tools it asks for. When the model keeps
asking after `--max-turns` turns, `jerry` reports it and stops with exit code 4.

---

## What a tool gets, and what comes back

The model sends a call as a name and JSON arguments. `jerry` calls the function with the
arguments as keywords, and what it returns is fed back to the model in the same session:

- a string goes back as it is,
- anything else goes back as JSON,
- a tool that returns nothing (one that only prints, say) goes back as `no result`.

Nothing is fatal. A tool that raises, an argument list that is not valid JSON, a name the
module does not have — each is reported to the model as the answer to its own call, so it
can react on it, and the run carries on. The exit code is still 0:

```
tool broken failed: ValueError: the weather service is down
no tool named translate
the arguments are not valid json: {city":
```

---

## What is printed

`jerry`' own lines about a tool call belong on stderr, and only with `-v`:

```
jerry: tools   get_weather, list_cities
jerry: tool  get_weather({"city": "Paris"})
jerry: tool  get_weather -> Sunny, 21C (0.03s)
```

A tool function may print on stdout or stderr itself, and that is fine, but it goes through
the same ordered writer as everything else, so nothing comes out of order: the call line,
whatever the tool printed while it ran, the result line. `stdout` is still the response, so
on a terminal where the meter owns stderr the tool's output is written once the run is over,
in the order it happened. A tool that prints on stdout prints into `-o FILE` as well when one
was given, and `-q` only silences jerry, never a tool.

---

## Tool log

`-L FILE` writes one line per call to FILE, appended, whatever `-v` is doing. It is there to
be read afterwards, so the fields are fixed:

```
2026-10-21T18:24:52+02:00 turn 1 get_weather {"city": "Paris"} -> mock weather in Paris (c) (0.00s)
2026-10-21T18:24:53+02:00 turn 2 broken {"city": "fail"} -> tool broken failed: ValueError: the weather service is down (0.00s)
```

timestamp (local, with the offset), turn, tool name, arguments as one sorted JSON object,
then the result or the failure, then how long the call took. The arguments are written sorted
so the same call reads the same way every time.

---

## Determinism

The tools are sent in name order, so the same modules send the same request whatever order
`-T` was given them in. `jerry` runs a tool when the model asks for one, so a run that calls
a tool is only as reproducible as the tool itself; a tool that reads a file, the clock, or the
network is not reproducible on its own.
