"""Refuse a release whose version is not declared everywhere it has to be.

Run this before tagging; release.yml runs it again with
the tag it is building, and with ``--wheel`` on the wheel it built. (Copied from itda-work/channels-nats.)

Deliberately dependency-free and stdlib-only, so it runs on any Python this
project supports, including 3.10 without tomllib.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def declared_version() -> str:
    """The one `version = "..."` line pyproject.toml is allowed to have."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.findall(r'^version = "(.+?)"$', text, re.MULTILINE)
    if len(found) != 1:
        raise SystemExit(f"pyproject.toml has {len(found)} version lines; expected exactly one")
    return found[0]


def problems(tag: str | None) -> list[str]:
    version = declared_version()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    found = []

    if tag is not None and tag != f"v{version}":
        found.append(f"tag {tag} does not match pyproject version {version}")

    if re.search(r'^\s*"License :: ', pyproject, re.MULTILINE):
        # PEP 639: PyPI rejects an upload carrying both. `twine check` does not catch it.
        found.append("drop the 'License :: ...' classifiers; they cannot accompany License-Expression")

    if f"## [{version}]" not in changelog:
        found.append(f"CHANGELOG.md has no '## [{version}]' section")

    unreleased = re.search(r"^## \[Unreleased\]\s*\n(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    if unreleased is not None and unreleased.group(1).strip():
        found.append(f"CHANGELOG.md still has entries under [Unreleased]; they belong in [{version}]")

    return found


# Files a wheel must carry besides the modules: the agent guide README.md and llms.txt point at.
PACKAGE_DATA = ["django_sqlalchemy_bridge/llms.txt"]


def wheel_problems(wheel: Path) -> list[str]:
    """What the built ``wheel`` lacks, or carries out of date, of :data:`PACKAGE_DATA`."""
    found = []
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for path in PACKAGE_DATA:
            if path not in names:
                found.append(f"{wheel.name} has no {path}")
            elif archive.read(path) != (ROOT / path).read_bytes():
                found.append(f"{wheel.name}: {path} differs from the source tree")
    return found


def main(argv: list[str]) -> int:
    if argv[:1] == ["--wheel"]:
        if len(argv) != 2:
            raise SystemExit("usage: release_check.py --wheel dist/<name>.whl")
        found = wheel_problems(Path(argv[1]))
        for problem in found:
            print(f"release check: {problem}", file=sys.stderr)
        if not found:
            print(f"release check: {Path(argv[1]).name} carries {', '.join(PACKAGE_DATA)}")
        return 1 if found else 0
    found = problems(argv[0] if argv else None)
    for problem in found:
        print(f"release check: {problem}", file=sys.stderr)
    if found:
        return 1
    print(f"release check: {declared_version()} is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
