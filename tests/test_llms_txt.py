"""The agent guide: django_sqlalchemy_bridge/llms.txt, its copy at the repository root, and the wheel (no database)."""

import importlib.util
import inspect
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import django_sqlalchemy_bridge

ROOT = Path(__file__).resolve().parent.parent
GUIDE = Path(django_sqlalchemy_bridge.__file__).parent / "llms.txt"
TEXT = GUIDE.read_text(encoding="utf-8")

RUN = {"capture_output": True, "text": True, "encoding": "utf-8", "check": True, "cwd": ROOT}

spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts" / "release_check.py")
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


def plain_signature(fn):
    """``fn``'s signature as llms.txt writes it: names and defaults, no annotations."""
    signature = inspect.signature(fn)
    parameters = [p.replace(annotation=inspect.Parameter.empty) for p in signature.parameters.values()]
    return str(signature.replace(parameters=parameters, return_annotation=inspect.Signature.empty))


def test_the_root_copy_is_the_package_file():
    assert (ROOT / "llms.txt").read_bytes() == GUIDE.read_bytes()


def test_it_follows_the_llms_txt_layout():
    lines = TEXT.splitlines()
    assert lines[0] == "# django-sqlalchemy-bridge"
    assert lines[1] == ""
    assert lines[2].startswith("> ")
    assert len(lines) <= 300


def test_every_public_name_appears():
    missing = [name for name in django_sqlalchemy_bridge.__all__ if f"`{name}`" not in TEXT and f"{name}(" not in TEXT]
    assert missing == []


def test_signatures_match_the_code():
    for name in django_sqlalchemy_bridge.__all__:
        obj = getattr(django_sqlalchemy_bridge, name)
        if inspect.isfunction(obj):
            assert f"{name}{plain_signature(obj)}" in TEXT, name


def test_a_changed_signature_is_caught():
    def execute(stmt, params=None, *, using="default", timeout=None):
        pass

    assert f"execute{plain_signature(execute)}" not in TEXT
    assert "execute(stmt, params=None, *, using='default')" in TEXT


def test_its_python_examples_compile():
    blocks = re.findall(r"^```python\n(.*?)^```$", TEXT, re.M | re.S)
    assert len(blocks) >= 5
    for block in blocks:
        compile(block, "llms.txt", "exec")


def test_the_module_docstring_points_at_it():
    first = django_sqlalchemy_bridge.__doc__.split("\n\n")[0]
    assert "llms.txt" in first
    assert "python -c" in django_sqlalchemy_bridge.__doc__


def test_the_read_command_prints_it():
    command = re.search(r'`(python -c "(.*?)")`', TEXT)
    assert command[1] in django_sqlalchemy_bridge.__doc__
    assert command[1] in (ROOT / "README.md").read_text(encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", command[2]], **RUN)
    assert out.stdout == TEXT + "\n"


def test_wheel_check_finds_a_missing_or_stale_guide(tmp_path):
    wheel = tmp_path / "x-0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("django_sqlalchemy_bridge/__init__.py", "")
    assert release_check.wheel_problems(wheel) == [f"{wheel.name} has no django_sqlalchemy_bridge/llms.txt"]
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("django_sqlalchemy_bridge/llms.txt", "old")
    assert release_check.wheel_problems(wheel) == [
        f"{wheel.name}: django_sqlalchemy_bridge/llms.txt differs from the source tree"
    ]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_the_built_wheel_carries_it(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], **RUN)
    [wheel] = tmp_path.glob("*.whl")
    assert release_check.wheel_problems(wheel) == []
