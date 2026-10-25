"""Scenario 4: query-building overhead, 5,000 lookups of one row by primary key.

Every lookup is a round trip, so the differences are what each tool spends
building, compiling and converting around it. The bridge has no compile cache
(#17): ``execute`` compiles the statement on every call. Two variants that are
not bridge API bound that cost from both sides: ``bridge_prepare_only``
compiles and converts without touching the database, and
``bridge_compiled_once_hypothetical`` compiles once through the bridge's
internal ``prepare()`` and then runs only the Django cursor and the result
converters, which is roughly what a compile cache could reach.
"""

from __future__ import annotations

import sqlalchemy as sa
from django.db import connection

from bench import data
from bench.harness import Variant, no_scope, run_variants, sa_rollback

N_ROWS = 20_000
LOOKUPS = 5_000

SCOPE = (
    f"Timed: {LOOKUPS} lookups of one row (7 columns) by primary key, each returning the row, inside one "
    "transaction (Django atomic(), or the SQLAlchemy connection's own). Untimed: the transaction itself."
)


def _pks() -> list[int]:
    # Spread over the table, every lookup a different row.
    return [(i * 7919) % N_ROWS + 1 for i in range(LOOKUPS)]


def variants(engine) -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import execute, table_for
    from django_sqlalchemy_bridge.execution import from_db, prepare

    items = table_for(Item)
    columns = [items.c[name] for name in data.COLUMNS]
    sa_table = data.SAItem.__table__
    sa_columns = [sa_table.c[name] for name in data.COLUMNS]
    pks = _pks()
    qn = connection.ops.quote_name
    raw_sql = f"SELECT {', '.join(qn(c) for c in data.COLUMNS)} FROM {qn(Item._meta.db_table)} WHERE {qn('id')} = %s"

    def check(result, _data, _handle):
        assert len(result) == LOOKUPS and all(result), "a lookup found no row"

    def django_get(_, __):
        return [Item.objects.get(pk=pk) for pk in pks]

    def django_values_list(_, __):
        return [Item.objects.values_list(*data.COLUMNS).get(pk=pk) for pk in pks]

    def raw_cursor(_, __):
        out = []
        with connection.cursor() as cursor:
            for pk in pks:
                cursor.execute(raw_sql, (pk,))
                out.append(cursor.fetchone())
        return out

    def bridge_build_each(_, __):
        return [execute(sa.select(*columns).where(items.c.id == pk))[0] for pk in pks]

    by_pk = sa.select(*columns).where(items.c.id == sa.bindparam("pk"))

    def bridge_bindparam(_, __):
        return [execute(by_pk, {"pk": pk})[0] for pk in pks]

    def bridge_prepare_only(_, __):
        return [prepare(by_pk, {"pk": pk}, connection).sql for pk in pks]

    def bridge_compiled_once(_, __):
        prepared = prepare(by_pk, {"pk": 0}, connection)
        out = []
        with connection.cursor() as cursor:
            for pk in pks:
                cursor.execute(prepared.sql, (pk,))
                out.append(from_db(cursor.fetchall(), prepared.converters, connection)[0])
        return out

    def sa_core_build_each(_, conn):
        return [conn.execute(sa.select(*sa_columns).where(sa_table.c.id == pk)).one() for pk in pks]

    sa_by_pk = sa.select(*sa_columns).where(sa_table.c.id == sa.bindparam("pk"))

    def sa_core_bindparam(_, conn):
        return [conn.execute(sa_by_pk, {"pk": pk}).one() for pk in pks]

    sa_scope = sa_rollback(engine)
    return [
        Variant("django_get", django_get, check=check, note="Item.objects.get(pk=pk)"),
        Variant(
            "django_values_list_get",
            django_values_list,
            check=check,
            note="Item.objects.values_list(*columns).get(pk=pk)",
        ),
        Variant("django_raw_cursor", raw_cursor, check=check, note="cursor.execute(sql, (pk,)); fetchone()"),
        Variant(
            "bridge_execute_build_each",
            bridge_build_each,
            check=check,
            note="execute(select(...).where(id == pk)): statement built and compiled per call",
        ),
        Variant(
            "bridge_execute_bindparam",
            bridge_bindparam,
            check=check,
            note="execute(stmt, {'pk': pk}) with stmt built once; still compiled per call (no cache)",
        ),
        Variant(
            "bridge_prepare_only",
            bridge_prepare_only,
            scope=no_scope,
            check=check,
            note="internal prepare() alone (compile + convert params), no database: the per-call overhead",
        ),
        Variant(
            "bridge_compiled_once_hypothetical",
            bridge_compiled_once,
            check=check,
            note="NOT bridge API: compiled once by internal prepare(), then Django cursor + converters only",
        ),
        Variant(
            "sqlalchemy_core_build_each",
            sa_core_build_each,
            scope=sa_scope,
            check=check,
            note="Connection.execute(select(...).where(id == pk)).one() (SQLAlchemy's compiled cache); own engine",
        ),
        Variant(
            "sqlalchemy_core_bindparam",
            sa_core_bindparam,
            scope=sa_scope,
            check=check,
            note="Connection.execute(stmt, {'pk': pk}).one(), stmt built once; own engine",
        ),
    ]


def run(engine, sizes=None) -> dict:
    data.seed(N_ROWS)
    print(f"  lookup x{LOOKUPS}", flush=True)
    return {
        "scope": SCOPE,
        "cases": [{"n": LOOKUPS, "variants": run_variants(variants(engine), connection.vendor, memory=False)}],
    }
