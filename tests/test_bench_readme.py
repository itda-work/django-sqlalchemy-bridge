"""The README's benchmark tables are the result files, rendered (no database, no benchmark run)."""

from bench import readme


def test_readme_tables_match_the_result_files():
    assert readme.differences(readme.README.read_text(encoding="utf-8")) == []


def test_every_committed_result_file_backs_the_readme():
    committed = {f"bench/results/{path.name}" for path in readme.RESULTS.glob("*.json")}
    assert committed == readme.referenced_files()
