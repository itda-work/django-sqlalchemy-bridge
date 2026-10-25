"""Render the README's benchmark tables from the result files, and check they match.

    uv run python -m bench.readme          # exit 1 if a README table differs from its result file
    uv run python -m bench.readme --write  # rewrite the tables in README.md

Every number in the "성능과 메모리" tables comes from here. A table sits
between ``<!-- bench:NAME -->`` and ``<!-- /bench:NAME -->``; ``TABLES`` says
which result file, variants and measurement it shows. Standard library only, so
the test suite can run the check without the benchmark dependencies.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
RESULTS = ROOT / "bench" / "results"

DATE = "2026-10-25"


def _file(db: str, scenario: str) -> str:
    return f"bench/results/{DATE}-{db}-{scenario}.json"


UPDATE_ROWS = [
    ("django_bulk_update", "Django `bulk_update()` (batch_size 기본값)"),
    ("django_bulk_update_batch1000", "Django `bulk_update(batch_size=1000)`"),
    ("pgbulk_update", "django-pgbulk `update()`"),
    ("sqlalchemy_orm_bulk_update", "SQLAlchemy ORM bulk UPDATE (별도 엔진)"),
    ("bridge_execute_executemany", "브리지 `execute(stmt, [params...])` (executemany)"),
    ("bridge_bulk_update", "브리지 `bulk_update()`"),
]
INSERT_ROWS = [
    ("django_bulk_create", "Django `bulk_create()` (batch_size 기본값)"),
    ("django_bulk_create_batch1000", "Django `bulk_create(batch_size=1000)`"),
    ("pgbulk_copy", "django-pgbulk `copy()`"),
    ("pgbulk_copy_binary", "django-pgbulk `copy(binary=True)`"),
    ("sqlalchemy_core_insert", "SQLAlchemy Core `insert()` (별도 엔진)"),
    ("sqlalchemy_core_insert_returning", "SQLAlchemy Core `insert().returning(id)` (별도 엔진)"),
    ("bridge_execute_executemany", "브리지 `execute(insert(t), [rows...])` (executemany)"),
    ("bridge_bulk_insert", "브리지 `bulk_insert()`"),
    ("bridge_bulk_insert_returning", "브리지 `bulk_insert(returning=['id'])`"),
]
READ_ROWS = [
    ("django_models", "Django 모델 객체 `list(qs)`"),
    ("django_values", "Django `values()`"),
    ("django_values_list", "Django `values_list()`"),
    ("sqlalchemy_orm_objects", "SQLAlchemy ORM 객체 (별도 엔진)"),
    ("sqlalchemy_core_rows", "SQLAlchemy Core `Row` (별도 엔진)"),
    ("bridge_execute_result_rows", "브리지 `execute_result().all()` (`Row`)"),
    ("bridge_execute_tuples", "브리지 `execute()` (튜플)"),
]
STREAM_ROWS = [
    ("django_iterator", "Django `values_list().iterator(chunk_size=2000)`"),
    ("bridge_stream", "브리지 `stream(chunk_size=2000)`"),
]
LOOKUP_ROWS = [
    ("django_raw_cursor", "Django 커서에 SQL 직접"),
    ("django_get", "Django `objects.get(pk=...)`"),
    ("django_values_list_get", "Django `values_list(...).get(pk=...)`"),
    ("sqlalchemy_core_bindparam", "SQLAlchemy Core, 문장 1회 생성 + `bindparam` (별도 엔진)"),
    ("sqlalchemy_core_build_each", "SQLAlchemy Core, 매번 문장 생성 (별도 엔진)"),
    ("bridge_execute_bindparam", "브리지 `execute()`, 문장 1회 생성 + `bindparam`"),
    ("bridge_execute_build_each", "브리지 `execute()`, 매번 문장 생성"),
    ("bridge_prepare_only", "(참고) 브리지 컴파일·파라미터 변환만, DB 없음"),
    ("bridge_compiled_once_hypothetical", "(가상) 컴파일 1회 후 커서·변환기만 — 캐시의 상한"),
]
ASYNC_ROWS = [
    ("sync_execute_sequential", "(기준) 동기 코드에서 `execute()` 1,000번"),
    ("aget_sequential", "`aget()` 1,000번 순차"),
    ("arun_sequential", "`arun()` 1,000번 순차 (호출마다 쿼리 1개)"),
    ("arun_sequential_conn_max_age_0", "같은 것, `CONN_MAX_AGE=0` (호출마다 재연결)"),
    ("aget_gather_100", "`gather` 100건 × `aget()`"),
    ("arun_gather_100", "`gather` 100건 × `arun()`"),
    ("aget_gather_1000", "`gather` 1,000건 × `aget()`"),
    ("arun_gather_1000", "`gather` 1,000건 × `arun()`"),
    ("unit5_aget_sequential", "작업 단위(쿼리 5개) 200개 순차, 쿼리마다 `aget()`"),
    ("unit5_arun_sequential", "작업 단위(쿼리 5개) 200개 순차, 단위마다 `arun()`"),
    ("unit5_aget_gather", "작업 단위 200개 `gather`, 쿼리마다 `aget()`"),
    ("unit5_arun_gather", "작업 단위 200개 `gather`, 단위마다 `arun()`"),
]
RANK_ROWS = [
    ("raw_cursor", "Django 커서에 SQL 직접"),
    ("django_window_no_cte", "Django ORM (CTE 없이 집계 + `Window`)"),
    ("django_cte", "Django + django-cte"),
    ("bridge", "브리지 (`select().cte()` + `rank().over()`)"),
]
RUNNING_ROWS = [
    ("django_window", "Django ORM `Window(Sum(...), partition_by=...)`"),
    ("bridge", "브리지 `func.sum().over(partition_by=...)`"),
]


def _sizes(*ns: int) -> list[tuple[str, dict]]:
    return [(f"{n:,}행", {"n": n}) for n in ns]


# name -> (result file, case columns [(header, case selector)], rows, metrics [(header, metric)])
TABLES: dict[str, dict] = {
    "pg-update": {
        "file": _file("postgres", "update"),
        "cases": _sizes(5_000, 50_000),
        "rows": UPDATE_ROWS,
        "metric": "ms",
    },
    "sqlite-update": {
        "file": _file("sqlite", "update"),
        "cases": _sizes(5_000, 50_000),
        "rows": UPDATE_ROWS,
        "metric": "ms",
    },
    "pg-insert": {
        "file": _file("postgres", "insert"),
        "cases": _sizes(20_000, 200_000),
        "rows": INSERT_ROWS,
        "metric": "ms",
    },
    "sqlite-insert": {
        "file": _file("sqlite", "insert"),
        "cases": _sizes(20_000, 200_000),
        "rows": INSERT_ROWS,
        "metric": "ms",
    },
    "pg-read": {
        "file": _file("postgres", "read"),
        "cases": [("", {"n": 20_000})],
        "rows": READ_ROWS,
        "metric": ["ms", "tracemalloc", "rss"],
    },
    "sqlite-read": {
        "file": _file("sqlite", "read"),
        "cases": [("", {"n": 20_000})],
        "rows": READ_ROWS,
        "metric": ["ms", "tracemalloc", "rss"],
    },
    "pg-stream": {
        "file": _file("postgres", "read"),
        "cases": [("", {"n": 20_000})],
        "rows": STREAM_ROWS,
        "metric": ["ms", "tracemalloc"],
    },
    "pg-lookup": {
        "file": _file("postgres", "lookup"),
        "cases": [("", {"n": 5_000})],
        "rows": LOOKUP_ROWS,
        "metric": ["ms", "us_per_call"],
    },
    "sqlite-lookup": {
        "file": _file("sqlite", "lookup"),
        "cases": [("", {"n": 5_000})],
        "rows": LOOKUP_ROWS,
        "metric": ["ms", "us_per_call"],
    },
    "pg-async": {
        "file": _file("postgres", "async"),
        "cases": [("", {"n": 1_000})],
        "rows": ASYNC_ROWS,
        "metric": ["ms", "us_per_query"],
    },
    "pg-report": {
        "file": _file("postgres", "report"),
        "cases": [("CTE + `RANK()` (500행)", {"query": "rank"})],
        "rows": RANK_ROWS,
        "metric": "ms",
    },
    "pg-running": {
        "file": _file("postgres", "report"),
        "cases": [("누적 합 `SUM() OVER` (20,000행)", {"query": "running"})],
        "rows": RUNNING_ROWS,
        "metric": "ms",
    },
}

METRIC_HEADERS = {
    "ms": "시간 (ms)",
    "tracemalloc": "tracemalloc 피크 (MiB)",
    "rss": "RSS 증가 (MiB)",
    "us_per_call": "호출당 (µs)",
    "us_per_query": "쿼리당 (µs)",
}


def _load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _case(result: dict, selector: dict) -> dict:
    found = [c for c in result["cases"] if all(c.get(k) == v for k, v in selector.items())]
    if len(found) != 1:
        raise LookupError(f"{result['scenario']}: {len(found)} cases match {selector}")
    return found[0]


def _num(value: float) -> str:
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _cell(measurement: dict | None, metric: str, case: dict) -> str:
    if measurement is None:
        return "—"
    if "skipped" in measurement:
        return "측정 안 함 ¹"
    if "error" in measurement:
        if "statement timeout" in measurement["error"]:
            return f"시간 초과 (>{measurement['timeout_s']:.0f} s)"
        mark = " ²" if "too many SQL variables" in measurement["error"] else ""
        return f"실패: `{measurement['error'].split(':')[0]}`{mark}"
    if metric == "ms":
        text = _num(measurement["best_s"] * 1000)
        return f"{text} (1회)" if measurement.get("cut_short") else text
    if metric == "tracemalloc":
        peak = measurement.get("tracemalloc_peak_bytes")
        return "—" if peak is None else f"{peak / 2**20:.1f}"
    if metric == "rss":
        rss = measurement.get("rss_delta_bytes_median")
        return "—" if rss is None else f"{rss / 2**20:.1f}"
    if metric == "us_per_call":
        return _num(measurement["best_s"] / case["n"] * 1e6)
    if metric == "us_per_query":
        return _num(measurement["best_s"] / measurement["queries"] * 1e6)
    raise ValueError(metric)


def render(name: str) -> str:
    spec = TABLES[name]
    result = _load(spec["file"])
    metrics = spec["metric"] if isinstance(spec["metric"], list) else [spec["metric"]]
    cases = [(header, _case(result, selector)) for header, selector in spec["cases"]]
    headers = []
    for header, _ in cases:
        for metric in metrics:
            label = METRIC_HEADERS[metric]
            headers.append(f"{header} {label}".strip() if len(cases) > 1 or header else label)
    lines = ["| | " + " | ".join(headers) + " |", "|---|" + "---:|" * len(headers)]
    for key, label in spec["rows"]:
        if all(key not in case["variants"] for _, case in cases):
            continue  # not run on this backend (django-pgbulk is PostgreSQL-only)
        cells = [_cell(case["variants"].get(key), metric, case) for _, case in cases for metric in metrics]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append(f"\n<sub>결과 파일: [`{spec['file']}`]({spec['file']})</sub>")
    return "\n".join(lines)


def environment(db: str) -> str:
    """The one-line environment of a backend's run, from its update result file."""
    result = _load(_file(db, "update"))
    e = result["environment"]
    p = e["packages"]
    m = e["machine"]
    memory = f", 메모리 {m['memory_bytes'] / 2**30:.0f} GiB" if m.get("memory_bytes") else ""
    return (
        f"{e['database']['server']} · Python {e['python']} · Django {p['django']} · SQLAlchemy {p['sqlalchemy']}"
        f" · psycopg {p['psycopg']} · django-pgbulk {p['django-pgbulk']} · django-cte {p['django-cte']}"
        f" · {m['cpu']} ({m['cpu_count']}코어{memory}) · {m['os']}"
        f" · 반복 {result['method']['repeat']}회(+예열 {result['method']['warmup']}회) 중 최솟값"
    )


def blocks() -> dict[str, str]:
    out = {name: render(name) for name in TABLES}
    for db in ("postgres", "sqlite"):
        out[f"env-{db}"] = environment(db)
    return out


def _pattern(name: str) -> re.Pattern:
    return re.compile(rf"(<!-- bench:{re.escape(name)} -->\n)(.*?)(<!-- /bench:{re.escape(name)} -->)", re.DOTALL)


def differences(text: str) -> list[str]:
    """Blocks of ``text`` (the README) that differ from what the result files render."""
    found = []
    for name, body in blocks().items():
        match = _pattern(name).search(text)
        if match is None:
            found.append(f"{name}: no <!-- bench:{name} --> block in README.md")
        elif match.group(2) != body + "\n":
            found.append(f"{name}: README.md differs from {TABLES.get(name, {}).get('file', 'the result files')}")
    return found


def referenced_files() -> set[str]:
    return {spec["file"] for spec in TABLES.values()} | {_file(db, "update") for db in ("postgres", "sqlite")}


def write(text: str) -> str:
    for name, body in blocks().items():
        text, count = _pattern(name).subn(lambda m, body=body: m.group(1) + body + "\n" + m.group(3), text)
        if count != 1:
            raise SystemExit(f"README.md needs exactly one <!-- bench:{name} --> ... <!-- /bench:{name} --> block")
    return text


def main(argv: list[str]) -> int:
    text = README.read_text(encoding="utf-8")
    if argv == ["--write"]:
        README.write_text(write(text), encoding="utf-8")
        return 0
    if argv:
        raise SystemExit("usage: python -m bench.readme [--write]")
    found = differences(text)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
