"""Scenario 2: INSERT many rows into an empty table."""

from __future__ import annotations

import sqlalchemy as sa
from django.db import connection

from bench import data
from bench.harness import Variant, run_variants, sa_rollback

SIZES = [20_000, 200_000]

SCOPE = (
    "Timed: the insert call alone. Untimed: building its input (model instances for Django and "
    "django-pgbulk, dicts for the bridge and SQLAlchemy) and the transaction, rolled back after each run "
    "(the table is empty before every run)."
)


def variants(n: int, engine) -> list[Variant]:
    from bench.benchapp.models import NewItem
    from django_sqlalchemy_bridge import bulk_insert, execute, table_for

    table = table_for(NewItem)
    sa_table = data.SANewItem.__table__

    def instances():
        return [NewItem(**data.new_row(i)) for i in range(1, n + 1)]

    def dicts():
        return [data.new_row(i) for i in range(1, n + 1)]

    def check(result, _data, _handle):
        assert NewItem.objects.count() == n

    def check_ids(result, _data, _handle):
        check(result, _data, _handle)
        assert len(result) == n

    def check_objs(objs, _data, _handle):
        check(objs, _data, _handle)
        assert all(o.pk is not None for o in objs)

    def check_sa(result, _data, conn):
        assert conn.scalar(sa.select(sa.func.count()).select_from(sa_table)) == n

    def pgbulk_copy(binary):
        def run(objs, _):
            import pgbulk

            pgbulk.copy(NewItem, objs, binary=binary)

        return run

    return [
        Variant(
            "django_bulk_create",
            lambda objs, _: NewItem.objects.bulk_create(objs),
            instances,
            check=check_objs,
            note="batch_size=None (default); sets each object's pk (RETURNING id on both backends)",
        ),
        Variant(
            "django_bulk_create_batch1000",
            lambda objs, _: NewItem.objects.bulk_create(objs, batch_size=1000),
            instances,
            check=check_objs,
            note="batch_size=1000",
        ),
        Variant(
            "pgbulk_copy",
            pgbulk_copy(False),
            instances,
            check=check,
            vendors=("postgresql",),
            note="pgbulk.copy(NewItem, objs) (COPY FROM STDIN, text); returns no ids",
        ),
        Variant(
            "pgbulk_copy_binary",
            pgbulk_copy(True),
            instances,
            check=check,
            vendors=("postgresql",),
            note="pgbulk.copy(NewItem, objs, binary=True)",
        ),
        Variant(
            "bridge_bulk_insert",
            lambda rows, _: bulk_insert(NewItem, rows),
            dicts,
            check=check,
            note="PostgreSQL: one INSERT ... SELECT * FROM unnest(...); SQLite: executemany",
        ),
        Variant(
            "bridge_bulk_insert_returning",
            lambda rows, _: bulk_insert(NewItem, rows, returning=["id"]),
            dicts,
            check=check_ids,
            note="returning=['id']; SQLite: multi-row VALUES in batches of bulk_batch_size()",
        ),
        Variant(
            "bridge_bulk_insert_copy",
            lambda rows, _: bulk_insert(NewItem, rows, method="copy"),
            dicts,
            check=check,
            vendors=("postgresql",),
            note="method='copy': COPY FROM STDIN (text), values from get_db_prep_save()",
        ),
        Variant(
            "bridge_execute_executemany",
            lambda rows, _: execute(sa.insert(table), rows),
            dicts,
            check=check,
            note="execute(insert(table), [dicts]) -> cursor.executemany",
        ),
        Variant(
            "sqlalchemy_core_insert",
            lambda rows, conn: conn.execute(sa.insert(sa_table), rows),
            dicts,
            scope=sa_rollback(engine),
            check=check_sa,
            note="Connection.execute(insert(table), [dicts]) (insertmanyvalues); own engine, not the bridge",
        ),
        Variant(
            "sqlalchemy_core_insert_returning",
            lambda rows, conn: conn.execute(sa.insert(sa_table).returning(sa_table.c.id), rows).all(),
            dicts,
            scope=sa_rollback(engine),
            check=check_sa,
            note="as above with .returning(id), rows fetched",
        ),
    ]


def run(engine, sizes=None) -> dict:
    from bench.benchapp.models import NewItem

    data.truncate(NewItem)
    cases = []
    for n in sizes or SIZES:
        print(f"  insert n={n}", flush=True)
        cases.append(
            {
                "n": n,
                "variants": run_variants(variants(n, engine), connection.vendor, after=lambda: data.vacuum(NewItem)),
            }
        )
    return {"scope": SCOPE, "cases": cases}
