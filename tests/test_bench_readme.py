"""The benchmark tables of README.md and docs/comparisons.md are the result files, rendered (no database, no run)."""

from bench import readme


def test_readme_tables_match_the_result_files():
    assert readme.differences(readme.README.read_text(encoding="utf-8")) == []


def test_comparisons_doc_tables_match_the_result_files():
    assert readme.differences(readme.COMPARISONS.read_text(encoding="utf-8"), readme.COMPARISONS) == []


def test_every_comparison_table_is_in_the_comparisons_doc_and_the_cards_in_the_readme():
    in_doc = set(readme.blocks(readme.COMPARISONS))
    in_readme = set(readme.blocks(readme.README))
    for request in readme.COMPARISONS_SPEC:
        assert {f"pg-compare-{request}", f"sqlite-compare-{request}"} <= in_doc
        assert (f"pg-compare-{request}" in in_readme) == (request in readme.CARDS)
    assert not any(name.startswith("sqlite-compare-") for name in in_readme)


def test_a_table_edited_or_shown_where_it_is_not_listed_is_caught():
    text = readme.COMPARISONS.read_text(encoding="utf-8")
    start = text.index("<!-- bench:pg-compare-tree -->")
    row = text.index("| 브리지: 재귀 CTE |", start)
    edited = text[:row] + text[row:].replace(" | 1 | ", " | 2 | ", 1)
    assert readme.differences(edited, readme.COMPARISONS) == [
        f"pg-compare-tree: docs/comparisons.md differs from {readme.TABLES['pg-compare-tree']['file']}"
    ]
    stray = text + "\n<!-- bench:pg-update -->\n<!-- /bench:pg-update -->\n"
    assert readme.differences(stray, readme.COMPARISONS) == [
        "pg-update: docs/comparisons.md shows a table it is not listed for in bench/readme.py"
    ]


def test_every_committed_result_file_backs_the_readme():
    committed = {f"bench/results/{path.name}" for path in readme.RESULTS.glob("*.json")}
    assert committed == readme.referenced_files()


def test_tables_read_the_newest_full_result_file(tmp_path, monkeypatch):
    for name in ("2026-10-25-sqlite-read.json", "2026-11-02-sqlite-read.json", "2026-12-01-sqlite-read-quick.json"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "2026-12-01-postgres-read.json").write_text("{}")
    monkeypatch.setattr(readme, "RESULTS", tmp_path)
    assert readme._file("sqlite", "read") == "bench/results/2026-11-02-sqlite-read.json"
