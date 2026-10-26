"""Keep the example code blocks of README.md and docs/drf.md equal to the tested code.

    uv run python -m tests.readme_examples          # exit 1 if a document example differs from its test
    uv run python -m tests.readme_examples --write  # copy the tested code into the documents

The tested code sits between ``# readme:NAME`` and ``# /readme:NAME`` comments
in the test file paired with each document (``PAIRS``); the document shows it,
dedented, as a ``python`` code block between ``<!-- example:NAME -->`` and
``<!-- /example:NAME -->``.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
EXAMPLES = ROOT / "tests" / "test_examples.py"
DRF_DOC = ROOT / "docs" / "drf.md"
DRF_RECIPES = ROOT / "tests" / "test_recipes_drf.py"
PAIRS = {README: EXAMPLES, DRF_DOC: DRF_RECIPES}

_SNIPPET = re.compile(r"^[ \t]*# readme:(?P<name>[\w-]+)\n(?P<body>.*?)^[ \t]*# /readme:(?P=name)\n", re.M | re.S)


def snippets(source: str | None = None, *, tested: Path = EXAMPLES) -> dict[str, str]:
    """Each tested snippet by name, dedented, as the document should show it."""
    source = tested.read_text(encoding="utf-8") if source is None else source
    found = {}
    for match in _SNIPPET.finditer(source):
        if match["name"] in found:
            raise ValueError(f"snippet {match['name']!r} appears twice in {tested.name}")
        # Module-level snippets end with the blank lines ruff puts before the closing comment.
        found[match["name"]] = textwrap.dedent(match["body"]).rstrip("\n") + "\n"
    return found


def _block(name: str, body: str) -> str:
    return f"<!-- example:{name} -->\n```python\n{body}```\n<!-- /example:{name} -->"


def _pattern(name: str | None = None) -> re.Pattern[str]:
    name = re.escape(name) if name else r"[\w-]+"
    return re.compile(rf"<!-- example:(?P<name>{name}) -->\n.*?<!-- /example:(?P=name) -->", re.S)


def differences(text: str, source: str | None = None, *, doc: Path = README) -> list[str]:
    """What in ``text`` (the document ``doc``) differs from the tested snippets."""
    doc_name, tested_name = doc.relative_to(ROOT).as_posix(), PAIRS[doc].relative_to(ROOT).as_posix()
    tested = snippets(source, tested=PAIRS[doc])
    shown = [match["name"] for match in _pattern().finditer(text)]
    found = [
        f"{name}: {doc_name} shows an example {tested_name} has no snippet for" for name in shown if name not in tested
    ]
    for name, body in tested.items():
        match = _pattern(name).search(text)
        if match is None:
            found.append(f"{name}: no <!-- example:{name} --> block in {doc_name}")
        elif match.group(0) != _block(name, body):
            found.append(f"{name}: {doc_name} differs from {tested_name} (run python -m tests.readme_examples --write)")
    return found


def write(text: str, source: str | None = None, *, doc: Path = README) -> str:
    for name, body in snippets(source, tested=PAIRS[doc]).items():
        text = _pattern(name).sub(lambda _: _block(name, body), text)
    return text


def main(argv: list[str]) -> int:
    if argv == ["--write"]:
        for doc in PAIRS:
            doc.write_text(write(doc.read_text(encoding="utf-8"), doc=doc), encoding="utf-8")
        return 0
    if argv:
        raise SystemExit("usage: python -m tests.readme_examples [--write]")
    found = [line for doc in PAIRS for line in differences(doc.read_text(encoding="utf-8"), doc=doc)]
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
