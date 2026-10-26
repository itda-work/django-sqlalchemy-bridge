"""README.md's example code is the tested code of tests/test_examples.py (no database)."""

from tests import readme_examples

README = readme_examples.README.read_text(encoding="utf-8")


def test_readme_examples_match_the_tested_code():
    assert readme_examples.differences(README) == []


def test_every_snippet_is_shown_in_the_readme():
    assert set(readme_examples.snippets()) == {"recursive-cte", "upsert", "upsert-mysql", "update-from"}


def test_an_edit_on_either_side_is_caught():
    source = readme_examples.EXAMPLES.read_text(encoding="utf-8")
    edited = source.replace('.cte("tree", recursive=True)', '.cte("tree2", recursive=True)')
    assert edited != source
    assert readme_examples.differences(README, edited) == [
        "recursive-cte: README.md differs from tests/test_examples.py (run python -m tests.readme_examples --write)"
    ]
    assert readme_examples.differences(README.replace("set_={", "set_ = {", 1)) == [
        "upsert: README.md differs from tests/test_examples.py (run python -m tests.readme_examples --write)"
    ]
    removed = README.replace("<!-- example:update-from -->", "", 1)
    assert readme_examples.differences(removed) == ["update-from: no <!-- example:update-from --> block in README.md"]
    assert readme_examples.write(README, edited) != README
    assert readme_examples.differences(readme_examples.write(README, edited), edited) == []
