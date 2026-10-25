"""The README's benchmark tables are the result files, rendered (no database, no benchmark run)."""

from bench import readme


def test_readme_tables_match_the_result_files():
    assert readme.differences(readme.README.read_text(encoding="utf-8")) == []


def test_every_committed_result_file_backs_the_readme():
    committed = {f"bench/results/{path.name}" for path in readme.RESULTS.glob("*.json")}
    assert committed == readme.referenced_files()


def test_tables_read_the_newest_full_result_file(tmp_path, monkeypatch):
    for name in ("2026-10-25-sqlite-read.json", "2026-11-02-sqlite-read.json", "2026-12-01-sqlite-read-quick.json"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "2026-12-01-postgres-read.json").write_text("{}")
    monkeypatch.setattr(readme, "RESULTS", tmp_path)
    assert readme._file("sqlite", "read") == "bench/results/2026-11-02-sqlite-read.json"
