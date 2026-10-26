"""Scenario 9: export 20,000 and 200,000 rows (7 columns) as CSV bytes (PostgreSQL).

Every variant writes the whole table as CSV with a header line into memory and
returns the bytes. ``copy_to()`` hands on the bytes PostgreSQL's ``COPY ... TO
STDOUT`` writes, so no row becomes a Python object; the others read rows (the
ORM's ``values_list()``, the bridge's ``execute()`` or ``stream()``) and write
them with ``csv.writer``. The text differs in detail (PostgreSQL writes
booleans as ``t``/``f`` and timestamps with ``+00``, ``csv.writer`` Python's
``str()``); the check is one line per row plus the header. tracemalloc counts
the output buffer too (about 1.3 MB for 20,000 rows). ``copy_to()`` needs
PostgreSQL with psycopg 3, so the scenario measures PostgreSQL only.
"""

from __future__ import annotations

import csv
import io

import sqlalchemy as sa
from django.db import connection

from bench import data
from bench.harness import Variant, no_scope, run_variants

SIZES = (20_000, 200_000)

SCOPE = (
    "Timed: reading every row and writing the CSV (header included) into an in-memory buffer, "
    "returned as bytes. No transaction. tracemalloc includes the output buffer."
)


def variants(n: int) -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import copy_to, execute, stream, table_for

    items = table_for(Item)
    stmt = sa.select(*(items.c[name] for name in data.COLUMNS))

    def check(result, _data, _handle):
        assert result.count(b"\n") == n + 1, result.count(b"\n")
        assert result.startswith(",".join(data.COLUMNS).encode() + b"\n"), result[:80]

    def write_csv(rows) -> bytes:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(data.COLUMNS)
        writer.writerows(rows)
        return out.getvalue().encode()

    def copied(_, __):
        out = io.BytesIO()
        out.writelines(copy_to(stmt))
        return out.getvalue()

    vendors = ("postgresql",)

    def variant(key, run, note):
        return Variant(key, run, scope=no_scope, check=check, vendors=vendors, note=note)

    return [
        variant(
            "django_values_list_csv",
            lambda _, __: write_csv(Item.objects.values_list(*data.COLUMNS)),
            "csv.writer over values_list(...)",
        ),
        variant(
            "django_iterator_csv",
            lambda _, __: write_csv(Item.objects.values_list(*data.COLUMNS).iterator(chunk_size=2000)),
            "csv.writer over values_list(...).iterator(chunk_size=2000)",
        ),
        variant("bridge_execute_csv", lambda _, __: write_csv(execute(stmt)), "csv.writer over execute(select(...))"),
        variant(
            "bridge_stream_csv",
            lambda _, __: write_csv(stream(stmt, chunk_size=2000)),
            "csv.writer over stream(select(...), chunk_size=2000)",
        ),
        variant("bridge_copy_to_csv", copied, "BytesIO().writelines(copy_to(select(...)))"),
    ]


def run(engine, sizes=None) -> dict:
    cases = []
    for n in sizes or SIZES:
        data.seed(n)
        print(f"  export n={n}", flush=True)
        cases.append({"n": n, "variants": run_variants(variants(n), connection.vendor)})
    return {"scope": SCOPE, "cases": cases}
