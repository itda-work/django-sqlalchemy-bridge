"""Bulk UPDATE and INSERT of many rows of a :func:`table_for` table in one go.

On PostgreSQL each call is one statement: every column travels as one array
parameter, cast to the column's Django database type, and ``unnest()`` turns
the arrays back into rows (the strategy Django's own ``bulk_create`` takes on
PostgreSQL). Elsewhere the same call runs ``cursor.executemany()``, or, for an
INSERT with ``RETURNING``, multi-row ``VALUES`` statements in batches the
backend accepts. The results are the same either way. ``batch_size`` splits the
rows into that many a statement (or an ``executemany()`` call); a call that runs
more than one statement does so in ``atomic(savepoint=False)``, as Django's
``bulk_create`` does, so a failure writes none of its rows.

Values convert as everywhere in the bridge: each value written goes through its
column's ``field.get_db_prep_save()``, each key :func:`bulk_update` matches rows
by as an ORM lookup's value (``None`` stays SQL ``NULL`` either way), and
``RETURNING`` columns through the ORM's converters. The SQL is assembled here rather than by
SQLAlchemy, with identifiers quoted by ``connection.ops.quote_name()``, and the
parameters are built a column at a time, not a row at a time.

Like the rest of the bridge these writes bypass the model layer: no ``save()``,
no ``pre_save`` / ``post_save`` signals, no field ``default`` or ``auto_now``.
So every column must be given (see :func:`bulk_insert`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from itertools import chain
from typing import Any

import sqlalchemy as sa
from django.db import DEFAULT_DB_ALIAS, NotSupportedError, connections, models, transaction
from django.db.backends.base.base import BaseDatabaseWrapper

from django_sqlalchemy_bridge.execution import _field_converters, check_returning, dialect_for, from_db, lookup_value
from django_sqlalchemy_bridge.tables import FieldType, table_for

__all__ = ["bulk_insert", "bulk_update"]

Row = Mapping[str, Any]
ColumnRef = str | sa.Column

# The alias of the unnest() rows in the PostgreSQL UPDATE.
_ROWS_ALIAS = "dsb_rows"


def bulk_update(
    table: sa.Table | type[models.Model],
    rows: Sequence[Row],
    *,
    key: str | Sequence[str] | None = None,
    columns: Sequence[ColumnRef] | None = None,
    batch_size: int | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> int:
    """Update many rows of ``table``, each matched by its ``key`` columns, and return the rowcount.

    ``table`` is a :func:`table_for` table (or the model, for its table).
    ``rows`` are mappings keyed by column name, as :func:`execute` takes them
    (``author_id``, a ``db_column``). ``key`` names the column or columns that
    pick the row to update, by default the primary key. ``columns`` names the
    columns to set; by default every column the first row has besides the key,
    and then every row must have exactly those. With ``columns`` given, other
    keys in the rows are ignored. A row lacking a key or set column, or two rows
    with the same key, raise ``ValueError`` before anything runs. A key that
    matches no row updates nothing; the rowcount counts the rows matched.

    PostgreSQL runs one ``UPDATE ... FROM unnest(...)`` statement; other
    backends ``cursor.executemany()``. A positive ``batch_size`` runs one such
    statement (or call) per that many rows instead; the rows are checked as a
    whole first and the rowcount is the sum. More than one statement runs in
    ``atomic(using, savepoint=False)``. No model signals are sent and
    ``auto_now`` is not applied.
    """
    _check_batch_size(batch_size)
    table = _table(table)
    rows = list(rows)
    keys = [_column(table, name) for name in _names(key)] if key is not None else list(table.primary_key.columns)
    if columns is None:
        if not rows:
            return 0
        sets = [_column(table, name) for name in rows[0] if name not in {c.key for c in keys}]
        _check_rows(rows, keys + sets, exact=True)
    else:
        sets = [_column(table, name) for name in columns]
        if overlap := [c.key for c in sets if c in keys]:
            raise ValueError(f"key column(s) {_quoted(overlap)} cannot also be set")
        _check_rows(rows, keys + sets, exact=False)
    if not sets:
        raise ValueError("nothing to update: the rows have no column besides the key")
    if not rows:
        return 0

    connection = connections[using]
    dialect_for(connection)  # refuses an unsupported backend
    key_values = [_db_values(column, rows, connection, lookup=True) for column in keys]
    _check_unique(list(zip(*key_values)), keys)
    set_values = [_db_values(column, rows, connection) for column in sets]

    qn = connection.ops.quote_name
    target = qn(table.name)
    batches = _batches(len(rows), batch_size)
    count = 0
    if connection.vendor == "postgresql":
        alias = qn(_ROWS_ALIAS)
        everything = keys + sets
        sql = (
            f"UPDATE {target} SET {', '.join(f'{qn(c.name)} = {alias}.{qn(c.name)}' for c in sets)}"
            f" FROM {_unnest(everything, connection)} AS {alias} ({', '.join(qn(c.name) for c in everything)})"
            f" WHERE {' AND '.join(f'{target}.{qn(c.name)} = {alias}.{qn(c.name)}' for c in keys)}"
        )
        with _atomic(using, len(batches) > 1), connection.cursor() as cursor:
            for batch in batches:
                cursor.execute(sql, [values[batch] for values in key_values + set_values])
                count += cursor.rowcount
        return count

    sql = (
        f"UPDATE {target} SET {', '.join(f'{qn(c.name)} = %s' for c in sets)}"
        f" WHERE {' AND '.join(f'{qn(c.name)} = %s' for c in keys)}"
    )
    params = list(zip(*set_values, *key_values))
    with _atomic(using, len(params) > 1), connection.cursor() as cursor:
        for batch in batches:
            cursor.executemany(sql, params[batch])
            count += cursor.rowcount
    return count


def bulk_insert(
    table: sa.Table | type[models.Model],
    rows: Sequence[Row],
    *,
    returning: Sequence[ColumnRef] | None = None,
    batch_size: int | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> int | list[tuple[Any, ...]]:
    """Insert ``rows`` into ``table`` and return the rowcount, or the ``returning`` columns of each row.

    ``table`` is a :func:`table_for` table (or the model, for its table).
    ``rows`` are mappings keyed by column name, all with the same keys. Every
    column of the table must be given, ``None`` for ``NULL``, except an
    auto-increment primary key and a column with a ``db_default``: Django field
    defaults and ``auto_now`` are not applied, so a column left out raises
    ``ValueError`` instead of silently storing ``NULL`` or failing on ``NOT NULL``.

    ``returning`` names columns (or gives :class:`sa.Column` objects of the
    table) to return; the result is then one tuple per row, converted by the
    Django fields, in the order of ``rows``. That order is what PostgreSQL and
    SQLite produce for these statements and what Django's ``bulk_create``
    relies on too, though neither database documents it as a guarantee.

    PostgreSQL runs one ``INSERT ... SELECT * FROM unnest(...)`` statement.
    Other backends run ``cursor.executemany()``, or with ``returning`` multi-row
    ``VALUES`` statements of at most ``connection.ops.bulk_batch_size()`` rows.
    A positive ``batch_size`` runs one statement (or ``executemany()`` call) per
    that many rows instead, never more than ``bulk_batch_size()`` with
    ``returning``; the rowcount is the sum and the returned rows stay in the
    order of ``rows``. More than one statement runs in
    ``atomic(using, savepoint=False)``. No model signals are sent.
    """
    _check_batch_size(batch_size)
    table = _table(table)
    rows = list(rows)
    returned = [_column(table, name) for name in returning] if returning is not None else None
    if not rows:
        return [] if returned is not None else 0
    given = [_column(table, name) for name in rows[0]]
    _check_rows(rows, given, exact=True)
    if left_out := [c.name for c in table.columns if c not in given and not _may_leave_out(c)]:
        raise ValueError(
            f"the rows leave out column(s) {_quoted(sorted(left_out))} of {table.name!r}; the bridge applies "
            "no Django defaults, so give every column (None for NULL)"
        )

    connection = connections[using]
    dialect_for(connection)  # refuses an unsupported backend
    if returned is not None:
        check_returning(connection)
    values = [_db_values(column, rows, connection) for column in given]

    qn = connection.ops.quote_name
    head = f"INSERT INTO {qn(table.name)} ({', '.join(qn(c.name) for c in given)})"
    tail = f" RETURNING {', '.join(qn(c.name) for c in returned)}" if returned is not None else ""
    count = 0
    out: list[tuple[Any, ...]] = []
    if connection.vendor == "postgresql":
        sql = f"{head} SELECT * FROM {_unnest(given, connection)}{tail}"
        batches = _batches(len(rows), batch_size)
        with _atomic(using, len(batches) > 1), connection.cursor() as cursor:
            for batch in batches:
                cursor.execute(sql, [column[batch] for column in values])
                if returned is None:
                    count += cursor.rowcount
                else:
                    out.extend(_convert(cursor.fetchall(), returned, connection))
        return count if returned is None else out

    params = list(zip(*values))
    if returned is None:
        with _atomic(using, len(params) > 1), connection.cursor() as cursor:
            for batch in _batches(len(params), batch_size):
                cursor.executemany(f"{head} VALUES ({', '.join(['%s'] * len(given))})", params[batch])
                count += cursor.rowcount
        return count
    if not connection.features.can_return_rows_from_bulk_insert:
        raise NotSupportedError(f"{connection.vendor} cannot return rows from a multi-row INSERT")
    size = max(connection.ops.bulk_batch_size([c.type.field for c in given], params), 1)
    batches = _batches(len(params), min(size, batch_size or size))
    placeholder = f"({', '.join(['%s'] * len(given))})"
    with _atomic(using, len(batches) > 1), connection.cursor() as cursor:
        for batch in batches:
            rows_of_batch = params[batch]
            cursor.execute(
                f"{head} VALUES {', '.join([placeholder] * len(rows_of_batch))}{tail}",
                list(chain.from_iterable(rows_of_batch)),
            )
            out.extend(_convert(cursor.fetchall(), returned, connection))
    return out


def _check_batch_size(batch_size: int | None) -> None:
    if batch_size is not None and (isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0):
        raise ValueError(f"batch_size must be a positive integer or None, got {batch_size!r}")


def _batches(count: int, batch_size: int | None) -> list[slice]:
    """Slices of ``count`` rows, ``batch_size`` at a time (all at once for ``None``)."""
    size = batch_size or count
    return [slice(start, start + size) for start in range(0, count, size)]


def _atomic(using: str, needed: bool) -> contextlib.AbstractContextManager:
    # As Django's bulk_create and bulk_update do: a call that runs several
    # statements (executemany runs one per row) writes all of its rows or none.
    return transaction.atomic(using=using, savepoint=False) if needed else contextlib.nullcontext()


def _table(table: sa.Table | type[models.Model]) -> sa.Table:
    if isinstance(table, type) and issubclass(table, models.Model):
        return table_for(table)
    if not (
        isinstance(table, sa.Table)
        and table.info.get("django_model") is not None
        and all(isinstance(c.type, FieldType) for c in table.columns)
    ):
        raise TypeError(f"expected a table built by table_for() or a Django model, got {table!r}")
    return table


def _names(key: str | Sequence[str]) -> list[str]:
    return [key] if isinstance(key, str) else list(key)


def _column(table: sa.Table, ref: ColumnRef) -> sa.Column:
    if isinstance(ref, sa.Column):
        if ref.table is not table:
            raise ValueError(f"{ref} is not a column of {table.name!r}")
        return ref
    column = table.columns.get(ref)
    if column is None:
        raise ValueError(f"table {table.name!r} has no column {ref!r}")
    return column


def _quoted(names: Sequence[str]) -> str:
    return ", ".join(repr(name) for name in names)


def _check_rows(rows: list[Row], columns: list[sa.Column], *, exact: bool) -> None:
    """Every row has a value for each of ``columns`` (and, if ``exact``, no other key)."""
    names = [c.key for c in columns]
    wanted = set(names)
    for i, row in enumerate(rows):
        if missing := [name for name in names if name not in row]:
            raise ValueError(f"row {i} has no value for column(s) {_quoted(missing)}")
        if exact and len(row) != len(wanted):
            extra = [name for name in row if name not in wanted]
            raise ValueError(f"row {i} has column(s) {_quoted(extra)} that row 0 does not")


def _may_leave_out(column: sa.Column) -> bool:
    field = column.type.field
    return (column.primary_key and column.autoincrement is True) or field.has_db_default()


def _db_values(
    column: sa.Column, rows: list[Row], connection: BaseDatabaseWrapper, *, lookup: bool = False
) -> list[Any]:
    """One column's values for every row, converted as the ORM writes them (or, ``lookup``, compares them)."""
    field = column.type.field
    name = column.key
    if lookup:
        return [None if (value := row[name]) is None else lookup_value(field, value, connection) for row in rows]
    return [None if (value := row[name]) is None else field.get_db_prep_save(value, connection) for row in rows]


def _check_unique(keys: list[tuple[Any, ...]], columns: list[sa.Column]) -> None:
    # PostgreSQL would update a row matched twice from just one, unpredictably
    # chosen, of the rows; executemany would apply both. Refuse either way.
    seen: dict[tuple[Any, ...], int] = {}
    for i, value in enumerate(keys):
        first = seen.setdefault(value, i)
        if first != i:
            raise ValueError(f"duplicate key {_quoted([c.key for c in columns])} = {value!r} in rows {first} and {i}")


def _unnest(columns: list[sa.Column], connection: BaseDatabaseWrapper) -> str:
    # As Django's bulk_create does, drop the type modifiers ("varchar[]" rather
    # than "varchar(50)[]") so the cast cannot truncate or round a value; the
    # assignment to the column then checks it like any INSERT or UPDATE.
    casts = [f"(%s)::{c.type.field.db_type(connection).split('(')[0]}[]" for c in columns]
    return f"unnest({', '.join(casts)})"


def _convert(
    rows: Sequence[Sequence[Any]], columns: list[sa.Column], connection: BaseDatabaseWrapper
) -> list[tuple[Any, ...]]:
    return from_db(rows, [_field_converters(c.type, connection) for c in columns], connection)
