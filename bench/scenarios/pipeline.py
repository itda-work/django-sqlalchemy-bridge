"""Scenario 8: several independent statements, one by one or in one ``execute_pipeline()`` (PostgreSQL).

``lookup``: 1,000 and 5,000 lookups of one row by primary key, each a
``(stmt, {"pk": pk})`` item of one statement built with ``bindparam``. One
``execute()`` per lookup waits for each round trip; ``execute_pipeline()``
sends every statement before reading any result. ``dashboard``: five
independent aggregates over the table, the queries a dashboard page runs once.
Both run inside a Django transaction that is rolled back; the lookups also
run outside one (autocommit), where ``execute_pipeline()`` opens its own
``atomic()`` and so adds a COMMIT round trip. Every variant must return the
same rows (checked). On other backends ``execute_pipeline()`` runs the
statements one by one, so the scenario measures PostgreSQL only.
"""

from __future__ import annotations

import sqlalchemy as sa
from django.db import connection

from bench import data
from bench.harness import Variant, no_scope, run_variants

N_ROWS = 20_000
SIZES = (1_000, 5_000)

SCOPE = (
    "Timed: the lookups or the five dashboard queries, each returning its rows. Inside one Django transaction "
    "that is rolled back (untimed), except the variants marked autocommit, which run outside any transaction."
)


def _pks(n: int) -> list[int]:
    # Spread over the table, every lookup a different row.
    return [(i * 7919) % N_ROWS + 1 for i in range(n)]


def _lookup_variants(n: int) -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import execute, execute_pipeline, table_for

    items = table_for(Item)
    by_pk = sa.select(*(items.c[name] for name in data.COLUMNS)).where(items.c.id == sa.bindparam("pk"))
    pks = _pks(n)
    expected = execute(sa.select(*(items.c[name] for name in data.COLUMNS)).where(items.c.id.in_(pks)))
    expected = sorted(expected)

    def check(result, _data, _handle):
        assert sorted(rows[0] for rows in result) == expected, "lookups returned other rows"

    def sequential(_, __):
        return [execute(by_pk, {"pk": pk}) for pk in pks]

    def pipelined(_, __):
        return execute_pipeline([(by_pk, {"pk": pk}) for pk in pks])

    vendors = ("postgresql",)
    return [
        Variant("execute_sequential", sequential, check=check, vendors=vendors, note="execute(stmt, {'pk': pk}) each"),
        Variant(
            "execute_pipeline",
            pipelined,
            check=check,
            vendors=vendors,
            note="execute_pipeline([(stmt, {'pk': pk}), ...])",
        ),
        Variant(
            "execute_sequential_autocommit",
            sequential,
            scope=no_scope,
            check=check,
            vendors=vendors,
            note="execute() each, outside a transaction",
        ),
        Variant(
            "execute_pipeline_autocommit",
            pipelined,
            scope=no_scope,
            check=check,
            vendors=vendors,
            note="execute_pipeline() outside a transaction: its own atomic(), so one COMMIT more",
        ),
    ]


def _dashboard_variants() -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import execute, execute_pipeline, table_for

    t = table_for(Item)
    statements = [
        sa.select(sa.func.count(), sa.func.sum(t.c.price)),
        sa.select(t.c.flag, sa.func.count(), sa.func.avg(t.c.amount)).group_by(t.c.flag).order_by(t.c.flag),
        sa.select(t.c.customer_id, sa.func.sum(t.c.price).label("total"))
        .group_by(t.c.customer_id)
        .order_by(sa.desc("total"), t.c.customer_id)
        .limit(10),
        sa.select(sa.func.min(t.c.created_at), sa.func.max(t.c.created_at)),
        sa.select(sa.func.count(sa.distinct(t.c.customer_id))).where(t.c.amount > 500),
    ]
    expected = [execute(stmt) for stmt in statements]

    def check(result, _data, _handle):
        assert result == expected, "dashboard returned other rows"

    def sequential(_, __):
        return [execute(stmt) for stmt in statements]

    def pipelined(_, __):
        return execute_pipeline(statements)

    vendors = ("postgresql",)
    return [
        Variant("execute_sequential", sequential, check=check, vendors=vendors, note="execute() per query"),
        Variant("execute_pipeline", pipelined, check=check, vendors=vendors, note="execute_pipeline(statements)"),
    ]


def run(engine, sizes=None) -> dict:
    data.seed(N_ROWS)
    cases = []
    for n in sizes or SIZES:
        print(f"  lookup x{n}", flush=True)
        variants = run_variants(_lookup_variants(n), connection.vendor, memory=False)
        cases.append({"query": "lookup", "n": n, "variants": variants})
    print("  dashboard", flush=True)
    variants = run_variants(_dashboard_variants(), connection.vendor, memory=False)
    cases.append({"query": "dashboard", "n": 5, "variants": variants})
    return {"scope": SCOPE, "cases": cases}
