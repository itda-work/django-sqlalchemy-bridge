"""Scenario 3: read 20,000 rows (7 columns), time and memory.

The "kept" variants build and hold the whole result, as a list; tracemalloc
counts what that allocates. Each is also run in a fresh child process to record
how much the process RSS grows while it holds the result. The "streamed"
variants walk the rows a chunk at a time and keep none of them.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys

import sqlalchemy as sa
from django.db import connection
from sqlalchemy.orm import Session

from bench import data
from bench.env import ROOT
from bench.harness import Variant, no_scope, run_variants

N = 20_000
RSS_RUNS = 3

SCOPE = (
    "Timed: running the query and building the full result (list of objects, dicts, tuples or rows), "
    "or for the streamed variants walking every row without keeping it. No transaction. tracemalloc counts "
    "what the call allocates; rss_* is measured in a fresh child process (see bench/child.py)."
)


def variants(engine) -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import execute, execute_result, stream, table_for

    items = table_for(Item)
    stmt = sa.select(*(items.c[name] for name in data.COLUMNS))
    sa_stmt = sa.select(*(data.SAItem.__table__.c[name] for name in data.COLUMNS))

    def check(result, _data, _handle):
        got = result if isinstance(result, int) else len(result)
        assert got == N, got

    def sa_core(_, __):
        with engine.connect() as conn:
            return conn.execute(sa_stmt).all()

    def sa_orm(_, __):
        with Session(engine) as session:
            return session.scalars(sa.select(data.SAItem)).all()

    def walk(rows):
        n = 0
        for _ in rows:
            n += 1
        return n

    def kept(key, run, note):
        return Variant(key, run, scope=no_scope, check=check, note=note, extra={"kept": True})

    def streamed(key, run, note):
        return Variant(key, run, scope=no_scope, check=check, note=note, extra={"kept": False})

    return [
        kept("django_models", lambda _, __: list(Item.objects.all()), "list(Item.objects.all())"),
        kept("django_values", lambda _, __: list(Item.objects.values()), "list(Item.objects.values())"),
        kept("django_values_list", lambda _, __: list(Item.objects.values_list()), "list(...values_list())"),
        kept("bridge_execute_tuples", lambda _, __: execute(stmt), "execute(select(...)) -> list of tuples"),
        kept(
            "bridge_execute_result_rows",
            lambda _, __: execute_result(stmt).all(),
            "execute_result(select(...)).all() -> SQLAlchemy Row objects",
        ),
        kept("sqlalchemy_core_rows", sa_core, "Connection.execute(select(...)).all(); own engine, not the bridge"),
        kept("sqlalchemy_orm_objects", sa_orm, "Session.scalars(select(SAItem)).all(); own engine, not the bridge"),
        streamed(
            "django_iterator",
            lambda _, __: walk(Item.objects.values_list().iterator(chunk_size=2000)),
            "values_list().iterator(chunk_size=2000), rows not kept",
        ),
        streamed(
            "bridge_stream",
            lambda _, __: walk(stream(stmt, chunk_size=2000)),
            "stream(select(...), chunk_size=2000), rows not kept",
        ),
    ]


def _rss(key: str) -> dict:
    """RSS growth of a fresh process while it holds variant ``key``'s result."""
    env = {**os.environ, "DSB_BENCH_DB_NAME": connection.settings_dict["NAME"]}
    runs = []
    for _ in range(RSS_RUNS):
        out = subprocess.run(
            [sys.executable, "-m", "bench.child", "read", key],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        runs.append(json.loads(out.stdout.strip().splitlines()[-1]))
    return {
        "rss_runs": runs,
        "rss_delta_bytes_median": statistics.median(r["rss_delta_bytes"] for r in runs),
        "maxrss_delta_bytes_median": statistics.median(r["maxrss_delta_bytes"] for r in runs),
    }


def run(engine, sizes=None) -> dict:
    data.seed(N)
    print(f"  read n={N}", flush=True)
    results = run_variants(variants(engine), connection.vendor)
    for key, result in results.items():
        if result.get("kept"):
            result.update(_rss(key))
            print(f"    {key:<34} rss +{result['rss_delta_bytes_median'] / 2**20:8.2f} MiB", flush=True)
    return {"scope": SCOPE, "cases": [{"n": N, "variants": results}]}
