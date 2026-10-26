"""Keep README.md's example code blocks equal to the tested code in tests/test_examples.py.

    uv run python -m tests.readme_examples          # exit 1 if a README example differs from its test
    uv run python -m tests.readme_examples --write  # copy the tested code into README.md

The tested code sits between ``# readme:NAME`` and ``# /readme:NAME`` comments
in tests/test_examples.py; README.md shows it, dedented, as a ``python`` code
block between ``<!-- example:NAME -->`` and ``<!-- /example:NAME -->``.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
EXAMPLES = ROOT / "tests" / "test_examples.py"

_SNIPPET = re.compile(r"^[ \t]*# readme:(?P<name>[\w-]+)\n(?P<body>.*?)^[ \t]*# /readme:(?P=name)\n", re.M | re.S)


def snippets(source: str | None = None) -> dict[str, str]:
    """Each tested snippet by name, dedented, as README.md should show it."""
    source = EXAMPLES.read_text(encoding="utf-8") if source is None else source
    found = {}
    for match in _SNIPPET.finditer(source):
        if match["name"] in found:
            raise ValueError(f"snippet {match['name']!r} appears twice in {EXAMPLES.name}")
        found[match["name"]] = textwrap.dedent(match["body"])
    return found


def _block(name: str, body: str) -> str:
    return f"<!-- example:{name} -->\n```python\n{body}```\n<!-- /example:{name} -->"


def _pattern(name: str | None = None) -> re.Pattern[str]:
    name = re.escape(name) if name else r"[\w-]+"
    return re.compile(rf"<!-- example:(?P<name>{name}) -->\n.*?<!-- /example:(?P=name) -->", re.S)


def differences(text: str, source: str | None = None) -> list[str]:
    """What in ``text`` (the README) differs from the tested snippets."""
    tested = snippets(source)
    shown = [match["name"] for match in _pattern().finditer(text)]
    found = [
        f"{name}: README.md shows an example tests/test_examples.py has no snippet for"
        for name in shown
        if name not in tested
    ]
    for name, body in tested.items():
        match = _pattern(name).search(text)
        if match is None:
            found.append(f"{name}: no <!-- example:{name} --> block in README.md")
        elif match.group(0) != _block(name, body):
            found.append(
                f"{name}: README.md differs from tests/test_examples.py (run python -m tests.readme_examples --write)"
            )
    return found


def write(text: str, source: str | None = None) -> str:
    for name, body in snippets(source).items():
        text = _pattern(name).sub(lambda _: _block(name, body), text)
    return text


def main(argv: list[str]) -> int:
    text = README.read_text(encoding="utf-8")
    if argv == ["--write"]:
        README.write_text(write(text), encoding="utf-8")
        return 0
    if argv:
        raise SystemExit("usage: python -m tests.readme_examples [--write]")
    found = differences(text)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
