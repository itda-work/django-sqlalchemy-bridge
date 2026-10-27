"""Join statements on different Django databases in Python, and split long ``IN`` lists.

A JOIN runs on one connection, so tables in different databases cannot meet in
one statement. :func:`join_across` does what Django's cross-database
``prefetch_related()`` does: it runs one statement, looks the other up by the
keys it returned with ``IN``, and pairs the rows in Python. :func:`execute_in`
is that lookup: one ``column IN (...)`` statement per chunk of values, so a
long list stays under the database's limits (Oracle takes 1,000 items in an
``IN`` list, SQLite a fixed number of parameters per statement).

Each chunk is its own statement: ``ORDER BY``, ``LIMIT`` and ``DISTINCT`` hold
within a chunk only. The statements run in whatever transaction each
connection has open; two databases share none.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.base import BaseDatabaseWrapper

from django_sqlalchemy_bridge.execution import _run, execute, prepare

__all__ = ["execute_in", "join_across"]

# The expanding parameter execute_in adds. Its values convert through the
# column's Django field as any IN list compared with the column does.
_VALUES = "dsb_in_values"

# The chunk when the database states no limit: the size Oracle allows.
DEFAULT_IN_CHUNK = 1000

_HOW = ("inner", "left")


def execute_in(
    stmt: sa.Select,
    column: sa.ColumnElement[Any],
    values: Iterable[Any],
    *,
    using: str = DEFAULT_DB_ALIAS,
    chunk_size: int | None = None,
) -> list[tuple[Any, ...]]:
    """Run ``stmt`` with ``column IN (values)`` added, a chunk of values at a time, and return all rows.

    ``None`` and repeated values are dropped (``IN (NULL)`` matches nothing);
    with no values left nothing runs and the result is ``[]``. The values
    convert as an ``in_()`` list does and the rows as :func:`execute` gives
    them, chunk after chunk in the order of the values' first appearance.

    ``chunk_size`` defaults to the smaller of the connection's
    ``ops.max_in_list_size()`` and ``features.max_query_params`` less the
    statement's own parameters, or 1000 when the database states neither.
    Each chunk is a statement of its own, so ``ORDER BY`` and ``LIMIT`` hold
    within a chunk, not across them.
    """
    if not isinstance(stmt, sa.Select):
        raise TypeError(f"execute_in() takes a Select statement, got {type(stmt).__name__}")
    if not isinstance(column, sa.ColumnElement):
        raise TypeError(f"execute_in() takes a column to compare, got {type(column).__name__}")
    _check_chunk_size(chunk_size)
    keys = list(dict.fromkeys(value for value in values if value is not None))
    if not keys:
        return []
    connection = connections[using]
    # One statement for every chunk: it compiles once, the chunks only expand its parameter.
    chunked = stmt.where(column.in_(sa.bindparam(_VALUES, expanding=True)))
    size = chunk_size or _default_chunk_size(chunked, keys[0], connection)
    rows: list[tuple[Any, ...]] = []
    for start in range(0, len(keys), size):
        rows += _run(prepare(chunked, {_VALUES: keys[start : start + size]}, connection), connection)[1]
    return rows


def _check_chunk_size(chunk_size: int | None) -> None:
    if chunk_size is not None and (isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0):
        raise ValueError(f"chunk_size must be a positive integer or None, got {chunk_size!r}")


def _default_chunk_size(stmt: sa.Select, sample: Any, connection: BaseDatabaseWrapper) -> int:
    limits = []
    if in_list := connection.ops.max_in_list_size():
        limits.append(in_list)
    if params := connection.features.max_query_params:
        # The statement's own parameters, counted as compiled with one value in the list.
        own = len(prepare(stmt, {_VALUES: [sample]}, connection).param_rows[0]) - 1
        limits.append(max(params - own, 1))
    return min(limits, default=DEFAULT_IN_CHUNK)


def join_across(
    outer: sa.Select,
    inner: sa.Select,
    on: tuple[Any, Any],
    *,
    using: str = DEFAULT_DB_ALIAS,
    inner_using: str | None = None,
    how: str = "inner",
    chunk_size: int | None = None,
) -> list[tuple[Any, ...]]:
    """Join ``outer`` (run on ``using``) with ``inner`` (run on ``inner_using``) in Python.

    ``on`` is ``(outer_key, inner_key)``: a column, a label or a result key
    name of each statement's selected columns. ``outer`` runs once; ``inner``
    runs through :func:`execute_in` with ``inner_key IN`` the outer key values,
    on ``inner_using`` (``using`` when not given). Each row is an outer row
    followed by an inner row, as a SQL JOIN gives it: outer rows in their
    order, one per matching inner row in fetch order. ``how="inner"`` drops
    outer rows without a match, ``how="left"`` keeps them with ``None`` in the
    inner places. An outer key ``None`` matches nothing.

    Keys pair by Python equality of the converted values, not by the
    database's collation (MySQL's ``_ci`` collations ignore case, this does
    not). The two statements do not share a transaction.
    """
    for stmt, side in ((outer, "outer"), (inner, "inner")):
        if not isinstance(stmt, sa.Select):
            raise TypeError(f"join_across() takes Select statements, got {type(stmt).__name__} as {side}")
    if how not in _HOW:
        raise ValueError(f"how must be 'inner' or 'left', got {how!r}")
    _check_chunk_size(chunk_size)
    outer_key, inner_key = _keys(on)
    outer_at, _ = _find(outer, outer_key, "outer")
    inner_at, inner_column = _find(inner, inner_key, "inner")

    outer_rows = execute(outer, using=using)
    inner_rows = execute_in(
        inner,
        inner_column,
        [row[outer_at] for row in outer_rows],
        using=inner_using or using,
        chunk_size=chunk_size,
    )
    matches: dict[Any, list[tuple[Any, ...]]] = {}
    for row in inner_rows:
        matches.setdefault(row[inner_at], []).append(row)
    unmatched = [(None,) * len(inner.selected_columns)] if how == "left" else []
    # No inner key is None (IN never matches NULL), so an outer None finds nothing.
    return [row + match for row in outer_rows for match in matches.get(row[outer_at], unmatched)]


def _keys(on: Any) -> tuple[Any, Any]:
    try:
        outer_key, inner_key = on
    except (TypeError, ValueError):
        raise ValueError(f"on must be a pair (outer_key, inner_key), got {on!r}") from None
    for key in (outer_key, inner_key):
        if isinstance(key, (tuple, list, sa.Tuple)):
            raise NotImplementedError("join_across() takes one key column per side, not a composite key")
    return outer_key, inner_key


def _find(stmt: sa.Select, key: Any, side: str) -> tuple[int, sa.ColumnElement[Any]]:
    """The position and column of ``key`` among ``stmt``'s selected columns.

    The same object first, then an equal one (``compare()``), then one equal
    once labels are taken off either side; a string is a result key name.
    """
    columns = list(stmt.selected_columns)
    if isinstance(key, str):
        found = [i for i, name in enumerate(stmt.selected_columns.keys()) if name == key]
        if len(found) > 1:
            raise ValueError(f"{side} key {key!r} names more than one selected column")
    elif isinstance(key, sa.ColumnElement):
        found = (
            [i for i, column in enumerate(columns) if column is key]
            or [i for i, column in enumerate(columns) if column.compare(key)]
            or [i for i, column in enumerate(columns) if _unlabeled(column).compare(_unlabeled(key))]
        )
    else:
        raise TypeError(f"{side} key must be a column, a label or a name, got {type(key).__name__}")
    if not found:
        raise ValueError(f"{side} key {key} is not among the {side} statement's selected columns")
    return found[0], columns[found[0]]


def _unlabeled(column: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
    return column.element if isinstance(column, sa.Label) else column
