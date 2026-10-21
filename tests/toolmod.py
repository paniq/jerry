"""A tool module for the tests: every function here that begins with tool_ is a tool.

    echo "what is the weather in Paris?" | ./jerry -T tests/toolmod.py -v

The tool functions answer with canned text so the tests can check what the model
was given back, and one of them fails, and one of them prints.
"""

import sys
import time


def tool_get_weather(city: str, units: str = "c") -> str:
    """Current weather for a city.

    Args:
        city: city name
        units: 'c' or 'f'
    """
    return "mock weather in %s (%s)" % (city, units)


def tool_list_cities() -> list[str]:
    """Cities known to the server."""
    return ["Paris", "Oslo"]


def tool_broken(city: str) -> str:
    """A tool that fails, so the model can react on it.

    Args:
        city: city name
    """
    raise ValueError("the weather service is down")


def tool_untyped(value):
    """A tool whose declaration says nothing about the type.

    Args:
        value: what to say
    """
    return str(value)


def tool_note(name: str, note: str = None) -> str:
    """A tool with a parameter that may be left out.

    Args:
        name: the name
        note: left out when there is nothing to add
    """
    return "%s/%s" % (name, note)


def tool_prints(note: str) -> str:
    """A tool that prints on both channels while it runs.

    Args:
        note: what to say
    """
    print("tool says hello on stdout")
    time.sleep(0.3)                     # a pause between each thing it says, so the
    print("tool says hello on stderr", file=sys.stderr)   # order can be read back
    time.sleep(0.3)
    return "printed " + note


def tool_slow(note: str) -> str:
    """A tool that takes its time, so a run can be interrupted while it is running.

    Args:
        note: what to say at the end
    """
    time.sleep(2)
    return "waited " + note


def tool_silent() -> None:
    """A tool that only prints, so what it returns is nothing."""
    print("tool says hello on stderr", file=sys.stderr)


def tool_anything(name: str, *rest) -> str:
    """A tool with parameters the model cannot be given.

    Args:
        name: the only parameter that is a parameter
    """
    return name


def helper(x):
    """Not a tool: the name does not begin with tool_."""
    return x
