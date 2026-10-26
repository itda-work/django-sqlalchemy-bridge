"""Render the benchmark tables of README.md and docs/comparisons.md from the result files, and check they match.

    uv run python -m bench.readme          # exit 1 if a table differs from its result file
    uv run python -m bench.readme --write  # rewrite the tables in README.md and docs/comparisons.md

Every number in the "성능과 메모리" tables and the ORM-versus-bridge
comparisons comes from here. A table sits between ``<!-- bench:NAME -->`` and
``<!-- /bench:NAME -->``; ``TABLES`` says which result file, variants and
measurement it shows, and which documents must show it. Standard library only,
so the test suite can run the check without the benchmark dependencies.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
COMPARISONS = ROOT / "docs" / "comparisons.md"
DOCS = (README, COMPARISONS)
RESULTS = ROOT / "bench" / "results"


def _file(db: str, scenario: str) -> str:
    """The newest full (not ``--quick``) result file of ``scenario`` on ``db``, by the date in its name."""
    found = sorted(RESULTS.glob(f"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-{db}-{scenario}.json"))
    if not found:
        raise FileNotFoundError(f"no bench/results/<date>-{db}-{scenario}.json")
    return f"bench/results/{found[-1].name}"


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
    ("bridge_prepare_only", "(참고) 브리지 내부 `prepare()`만 (캐시 적중 + 파라미터 변환), DB 없음"),
    ("bridge_prepared_once", "(참고) 내부 `prepare()` 1회 후 커서 하나로 실행·변환만"),
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
BATCH_INSERT_ROWS = [
    ("insert_none", "`bulk_insert()` (batch_size=None)"),
    ("insert_batch10000", "`bulk_insert(batch_size=10_000)`"),
    ("insert_batch50000", "`bulk_insert(batch_size=50_000)`"),
    ("insert_batch200000", "`bulk_insert(batch_size=200_000)`"),
    ("insert_returning_none", "`bulk_insert(returning=['id'])` (batch_size=None)"),
    ("insert_returning_batch50000", "`bulk_insert(returning=['id'], batch_size=50_000)`"),
]
BATCH_UPDATE_ROWS = [
    ("update_none", "`bulk_update()` (batch_size=None)"),
    ("update_batch10000", "`bulk_update(batch_size=10_000)`"),
    ("update_batch50000", "`bulk_update(batch_size=50_000)`"),
    ("update_batch200000", "`bulk_update(batch_size=200_000)`"),
]
PIPELINE_ROWS = [
    ("execute_sequential", "`execute()`를 문장마다 (트랜잭션 안)"),
    ("execute_pipeline", "`execute_pipeline()` (트랜잭션 안)"),
    ("execute_sequential_autocommit", "`execute()`를 문장마다 (트랜잭션 밖)"),
    ("execute_pipeline_autocommit", "`execute_pipeline()` (트랜잭션 밖, 자체 `atomic()`)"),
]
RUNNING_ROWS = [
    ("django_window", "Django ORM `Window(Sum(...), partition_by=...)`"),
    ("bridge", "브리지 `func.sum().over(partition_by=...)`"),
]


TREE_ROWS = [
    ("orm_levels", "ORM: 한 단계에 쿼리 하나씩"),
    ("orm_python", "ORM: 카테고리 전체를 읽어 파이썬에서"),
    ("bridge", "브리지: 재귀 CTE"),
]
LATEST_ROWS = [
    ("orm_window", "ORM: `Window(RowNumber())`로 거르기"),
    ("orm_prefetch", "ORM: 잘라 낸 `Prefetch`"),
    ("bridge", "브리지: `LATERAL`"),
]
UPSERT_ROWS = [
    ("orm_loop", "ORM: 행마다 `update()`, 없으면 `create()`"),
    ("orm_bulk", "ORM: 잠가 읽기 + `bulk_create(update_conflicts=True)`"),
    ("bridge", "브리지: `ON CONFLICT DO UPDATE … RETURNING`"),
]
PRICES_ROWS = [
    ("orm_subquery", "ORM: 읽기 + `Subquery` UPDATE"),
    ("orm_bulk_update", "ORM: 읽기 + `bulk_update(batch_size=1000)`"),
    ("bridge", "브리지: `UPDATE … FROM … RETURNING`"),
]
ARCHIVE_ROWS = [
    ("orm", "ORM: 읽기 + `bulk_create()` + `delete()`"),
    ("bridge", "브리지: `INSERT … SELECT` + `DELETE`"),
    ("bridge_cte", "브리지: 데이터를 바꾸는 CTE"),
]
REPORT_ROWS = [
    ("orm", "ORM: 상관 `Subquery` + `Window`"),
    ("bridge", "브리지: CTE + 윈도 함수"),
]


def _sizes(*ns: int) -> list[tuple[str, dict]]:
    return [(f"{n:,}행", {"n": n}) for n in ns]


# request -> (rows, [(header, case selector)]); a header is formatted with its case's fields.
COMPARISONS_SPEC = {
    "tree": (TREE_ROWS, [(f"깊이 {d}", {"depth": d}) for d in (3, 5, 7)]),
    "latest": (LATEST_ROWS, [("상품 {products:,}", {"products": n}) for n in (1_000, 10_000)]),
    "upsert": (UPSERT_ROWS, [("{rows:,}행", {"rows": n}) for n in (100, 1_000, 5_000)]),
    "prices": (PRICES_ROWS, [("상품 {products:,}", {"products": n}) for n in (1_000, 10_000)]),
    "archive": (ARCHIVE_ROWS, [("판매 {sales:,}", {"sales": n}) for n in (10_000, 100_000)]),
    "report": (REPORT_ROWS, [("상품 {products:,}", {"products": n}) for n in (1_000, 10_000)]),
}
_CMP_METRICS = [("queries", "쿼리"), ("ms", "시간 (ms)")]
# The comparisons README.md shows as cards, PostgreSQL only.
CARDS = ("tree", "prices", "archive", "upsert")


def _comparison(db: str, request: str, docs: tuple[Path, ...]) -> dict:
    """A table of every size's queries and time, and the largest size's tracemalloc peak."""
    rows, cases = COMPARISONS_SPEC[request]
    cases = [(header, {"request": request, **selector}) for header, selector in cases]
    columns = [(f"{header} {label}", selector, metric) for header, selector in cases for metric, label in _CMP_METRICS]
    largest, selector = cases[-1]
    columns.append((f"{largest} tracemalloc 피크 (MiB)", selector, "tracemalloc"))
    return {"file": _file(db, "compare"), "columns": columns, "rows": rows, "docs": docs}


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
    "pg-pipeline": {
        "file": _file("postgres", "pipeline"),
        "cases": [("PK 조회 1,000번", {"query": "lookup", "n": 1_000}), ("5,000번", {"query": "lookup", "n": 5_000})],
        "rows": PIPELINE_ROWS,
        "metric": "ms",
    },
    "pg-pipeline-dashboard": {
        "file": _file("postgres", "pipeline"),
        "cases": [("집계 쿼리 5개 1회", {"query": "dashboard"})],
        "rows": PIPELINE_ROWS,
        "metric": "ms",
    },
    "pg-batch-insert": {
        "file": _file("postgres", "batch"),
        "cases": [("", {"op": "insert", "n": 1_000_000})],
        "rows": BATCH_INSERT_ROWS,
        "metric": ["ms", "tracemalloc", "server"],
    },
    "pg-batch-update": {
        "file": _file("postgres", "batch"),
        "cases": [("", {"op": "update", "n": 1_000_000})],
        "rows": BATCH_UPDATE_ROWS,
        "metric": ["ms", "tracemalloc", "server"],
    },
    "sqlite-batch-insert": {
        "file": _file("sqlite", "batch"),
        "cases": [("", {"op": "insert", "n": 1_000_000})],
        "rows": BATCH_INSERT_ROWS,
        "metric": ["ms", "tracemalloc"],
    },
    "sqlite-batch-update": {
        "file": _file("sqlite", "batch"),
        "cases": [("", {"op": "update", "n": 1_000_000})],
        "rows": BATCH_UPDATE_ROWS,
        "metric": ["ms", "tracemalloc"],
    },
}
for _request in COMPARISONS_SPEC:
    TABLES[f"pg-compare-{_request}"] = _comparison(
        "postgres", _request, (README, COMPARISONS) if _request in CARDS else (COMPARISONS,)
    )
    TABLES[f"sqlite-compare-{_request}"] = _comparison("sqlite", _request, (COMPARISONS,))

METRIC_HEADERS = {
    "ms": "시간 (ms)",
    "tracemalloc": "tracemalloc 피크 (MiB)",
    "rss": "RSS 증가 (MiB)",
    "us_per_call": "호출당 (µs)",
    "us_per_query": "쿼리당 (µs)",
    "queries": "쿼리",
    "server": "서버 백엔드 메모리 증가 (MiB)",
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
    if metric == "queries":
        return f"{measurement['queries']:,}"
    if metric == "ms":
        text = _num(measurement["best_s"] * 1000)
        return f"{text} (1회)" if measurement.get("cut_short") else text
    if metric == "tracemalloc":
        peak = measurement.get("tracemalloc_peak_bytes")
        return "—" if peak is None else f"{peak / 2**20:.1f}"
    if metric == "rss":
        rss = measurement.get("rss_delta_bytes_median")
        return "—" if rss is None else f"{rss / 2**20:.1f}"
    if metric == "server":
        value = measurement.get("server", {}).get("rss_anon_peak_delta_bytes")
        return "—" if value is None else f"{value / 2**20:.1f}"
    if metric == "us_per_call":
        return _num(measurement["best_s"] / case["n"] * 1e6)
    if metric == "us_per_query":
        return _num(measurement["best_s"] / measurement["queries"] * 1e6)
    raise ValueError(metric)


def _columns(spec: dict, result: dict) -> list[tuple[str, dict, str]]:
    """``(header, case, metric)`` of each column: ``spec["columns"]``, or every case by every metric."""
    if "columns" in spec:
        found = []
        for header, selector, metric in spec["columns"]:
            case = _case(result, selector)
            found.append((header.format(**case), case, metric))
        return found
    metrics = spec["metric"] if isinstance(spec["metric"], list) else [spec["metric"]]
    cases = [(header, _case(result, selector)) for header, selector in spec["cases"]]
    columns = []
    for header, case in cases:
        for metric in metrics:
            label = METRIC_HEADERS[metric]
            columns.append((f"{header} {label}".strip() if len(cases) > 1 or header else label, case, metric))
    return columns


def render(name: str, doc: Path = README) -> str:
    """Table ``name`` as document ``doc`` shows it (the result file linked relative to ``doc``)."""
    spec = TABLES[name]
    result = _load(spec["file"])
    columns = _columns(spec, result)
    lines = ["| | " + " | ".join(header for header, _, _ in columns) + " |", "|---|" + "---:|" * len(columns)]
    for key, label in spec["rows"]:
        if all(key not in case["variants"] for _, case, _ in columns):
            continue  # not run on this backend (django-pgbulk is PostgreSQL-only)
        cells = [_cell(case["variants"].get(key), metric, case) for _, case, metric in columns]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    link = Path(os.path.relpath(ROOT / spec["file"], doc.parent)).as_posix()
    lines.append(f"\n<sub>결과 파일: [`{spec['file']}`]({link})</sub>")
    return "\n".join(lines)


def environment(db: str, scenario: str = "update") -> str:
    """The one-line environment of a backend's run, from its ``scenario`` (by default update) result file."""
    result = _load(_file(db, scenario))
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


def blocks(doc: Path = README) -> dict[str, str]:
    """The blocks document ``doc`` must show, by name."""
    out = {name: render(name, doc) for name, spec in TABLES.items() if doc in spec.get("docs", (README,))}
    if doc == README:
        for db in ("postgres", "sqlite"):
            out[f"env-{db}"] = environment(db)
        out["env-postgres-batch"] = environment("postgres", "batch")
    out["env-postgres-compare"] = environment("postgres", "compare")
    if doc == COMPARISONS:
        out["env-sqlite-compare"] = environment("sqlite", "compare")
    return out


def _pattern(name: str) -> re.Pattern:
    return re.compile(rf"(<!-- bench:{re.escape(name)} -->\n)(.*?)(<!-- /bench:{re.escape(name)} -->)", re.DOTALL)


_ANY = re.compile(r"<!-- bench:(?P<name>[\w-]+) -->")


def differences(text: str, doc: Path = README) -> list[str]:
    """Blocks of ``text`` (the document ``doc``) that differ from what the result files render."""
    doc_name = doc.relative_to(ROOT).as_posix()
    expected = blocks(doc)
    found = [
        f"{name}: {doc_name} shows a table it is not listed for in bench/readme.py"
        for name in (m["name"] for m in _ANY.finditer(text))
        if name not in expected
    ]
    for name, body in expected.items():
        match = _pattern(name).search(text)
        if match is None:
            found.append(f"{name}: no <!-- bench:{name} --> block in {doc_name}")
        elif match.group(2) != body + "\n":
            found.append(f"{name}: {doc_name} differs from {TABLES.get(name, {}).get('file', 'the result files')}")
    return found


def referenced_files() -> set[str]:
    return (
        {spec["file"] for spec in TABLES.values()}
        | {_file(db, "update") for db in ("postgres", "sqlite")}
        | {_file("postgres", "batch")}
        | {_file(db, "compare") for db in ("postgres", "sqlite")}
    )


def write(text: str, doc: Path = README) -> str:
    doc_name = doc.relative_to(ROOT).as_posix()
    for name, body in blocks(doc).items():
        text, count = _pattern(name).subn(lambda m, body=body: m.group(1) + body + "\n" + m.group(3), text)
        if count != 1:
            raise SystemExit(f"{doc_name} needs exactly one <!-- bench:{name} --> ... <!-- /bench:{name} --> block")
    return text


def main(argv: list[str]) -> int:
    if argv == ["--write"]:
        for doc in DOCS:
            doc.write_text(write(doc.read_text(encoding="utf-8"), doc), encoding="utf-8")
        return 0
    if argv:
        raise SystemExit("usage: python -m bench.readme [--write]")
    found = [line for doc in DOCS for line in differences(doc.read_text(encoding="utf-8"), doc)]
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
