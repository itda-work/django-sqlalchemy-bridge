"""README.md's example code is the tested code of tests/test_examples.py (no database)."""

import pytest

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


def test_drf_doc_examples_match_the_tested_recipes():
    drf = readme_examples.DRF_DOC.read_text(encoding="utf-8")
    assert readme_examples.differences(drf, doc=readme_examples.DRF_DOC) == []


def test_every_recipe_is_shown_in_the_drf_doc():
    assert set(readme_examples.snippets(tested=readme_examples.DRF_RECIPES)) == {
        "drf-list",
        "drf-pagination",
        "drf-create",
        "drf-atomic",
        "drf-template",
        "drf-raw",
    }


DIFFERS = "docs/drf.md differs from tests/test_recipes_drf.py (run python -m tests.readme_examples --write)"


def test_an_edit_on_either_side_of_the_drf_doc_is_caught():
    drf = readme_examples.DRF_DOC.read_text(encoding="utf-8")
    source = readme_examples.DRF_RECIPES.read_text(encoding="utf-8")
    edited = source.replace("default_limit = 20", "default_limit = 50")
    assert edited != source
    assert readme_examples.differences(drf, edited, doc=readme_examples.DRF_DOC) == [f"drf-pagination: {DIFFERS}"]
    assert readme_examples.differences(
        drf.replace(".mappings().one()", ".mappings().first()"), doc=readme_examples.DRF_DOC
    ) == [f"drf-create: {DIFFERS}"]
    removed = drf.replace("<!-- example:drf-template -->", "", 1)
    assert readme_examples.differences(removed, doc=readme_examples.DRF_DOC) == [
        "drf-template: no <!-- example:drf-template --> block in docs/drf.md"
    ]


COMPARISONS_DOC = readme_examples.COMPARISONS_DOC.read_text(encoding="utf-8")
COMPARISONS = readme_examples.COMPARISONS.read_text(encoding="utf-8")
CMP_DIFFERS = "docs/comparisons.md differs from tests/comparisons.py (run python -m tests.readme_examples --write)"


def test_comparisons_doc_examples_match_the_tested_code():
    assert readme_examples.differences(COMPARISONS_DOC, doc=readme_examples.COMPARISONS_DOC) == []


def test_every_comparison_is_shown_in_the_comparisons_doc():
    assert set(readme_examples.snippets(tested=readme_examples.COMPARISONS)) == {
        "cmp-tables",
        "cmp-tree-orm-levels",
        "cmp-tree-orm-python",
        "cmp-tree-bridge",
        "cmp-top2-orm-window",
        "cmp-top2-orm-prefetch",
        "cmp-top2-bridge",
        "cmp-upsert-orm-loop",
        "cmp-upsert-orm-bulk",
        "cmp-upsert-bridge",
        "cmp-prices-orm-subquery",
        "cmp-prices-orm-bulk-update",
        "cmp-prices-bridge",
        "cmp-archive-orm",
        "cmp-archive-bridge",
        "cmp-archive-bridge-cte",
        "cmp-report-orm",
        "cmp-report-bridge",
    }


def test_an_edit_on_either_side_of_the_comparisons_doc_is_caught():
    doc = readme_examples.COMPARISONS_DOC
    edited = COMPARISONS.replace('.cte("tree", recursive=True)', '.cte("subtree", recursive=True)')
    assert edited != COMPARISONS
    assert readme_examples.differences(COMPARISONS_DOC, edited, doc=doc) == [f"cmp-tree-bridge: {CMP_DIFFERS}"]
    assert readme_examples.differences(
        COMPARISONS_DOC.replace("product.price = product.new", "product.price = product.new_price", 1), doc=doc
    ) == [f"cmp-prices-orm-bulk-update: {CMP_DIFFERS}"]
    removed = COMPARISONS_DOC.replace("<!-- example:cmp-report-orm -->", "", 1)
    assert readme_examples.differences(removed, doc=doc) == [
        "cmp-report-orm: no <!-- example:cmp-report-orm --> block in docs/comparisons.md"
    ]


def test_the_readme_shows_some_comparisons_as_tested():
    shown = set(readme_examples._pattern().findall(README))
    comparisons = set(readme_examples.snippets(tested=readme_examples.COMPARISONS))
    assert shown & comparisons, "the README shows no comparison card"
    assert shown & comparisons < comparisons
    # A comparison the README shows must match tests/comparisons.py; one it leaves out is not required.
    (name, *_) = sorted(shown & comparisons)
    readme_side = README.replace(f"<!-- example:{name} -->\n```python\n", f"<!-- example:{name} -->\n```python\n#\n", 1)
    assert readme_examples.differences(readme_side) == [
        f"{name}: README.md differs from tests/comparisons.py (run python -m tests.readme_examples --write)"
    ]
    edited = COMPARISONS.replace("[(total,)] = execute(subtree_revenue", "[(total,)] = execute(subtree_revenue_stmt")
    assert edited != COMPARISONS
    assert readme_examples.differences(README, extra={readme_examples.COMPARISONS: edited}) == [
        "cmp-tree-bridge: README.md differs from tests/comparisons.py (run python -m tests.readme_examples --write)"
    ]


def test_snippet_names_must_not_repeat_across_the_files_of_a_document():
    clash = COMPARISONS.replace("# readme:cmp-tables", "# readme:upsert").replace(
        "# /readme:cmp-tables", "# /readme:upsert"
    )
    with pytest.raises(ValueError, match="'upsert'"):
        readme_examples.differences(README, extra={readme_examples.COMPARISONS: clash})
