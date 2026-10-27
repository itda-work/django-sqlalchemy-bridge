"""Join statements on different Django databases in Python, and split long ``IN`` lists.

A JOIN runs on one connection, so tables in different databases cannot meet in
one statement. :func:`join_across` does what Django's cross-database
``prefetch_related()`` does: it runs one statement, looks the other up by the
keys it returned with ``IN``, and pairs the rows in Python. :func:`execute_in`
is that lookup: one ``column IN (...)`` statement per chunk of values, so a
long list stays under the database's limits: SQLite takes a fixed number of
parameters per statement, Oracle up to 19c 1,000 items in an ``IN`` list
(``ORA-01795``; 23ai takes 65,535, but Django's Oracle backend still gives
``max_in_list_size()`` 1,000, and so the bridge splits at 1,000).

Each chunk is its own statement: ``ORDER BY``, ``LIMIT`` and ``DISTINCT`` hold
within a chunk only. The statements run in whatever transaction each
connection has open; two databases share none.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.base import BaseDatabaseWrapper

from django_sqlalchemy_bridge.execution import Params, _run, execute, prepare

__all__ = ["execute_in", "join_across"]

# The expanding parameter execute_in adds. Its values convert through the
# column's Django field as any IN list compared with the column does.
_VALUES = "dsb_in_values"

# The chunk when the database states no limit: Oracle's IN list limit up to 19c.
DEFAULT_IN_CHUNK = 1000

_HOW = ("inner", "left")

# Statement -> (column, the statement with column IN the expanding parameter).
# Built once per statement and column, so a statement run again with other
# values or params compiles once (the compile cache is keyed by the statement
# object). One column per statement: a column expression built per call
# replaces the last one rather than piling up. The chunked statement does not
# hold the caller's statement, so the entry goes with it.
_chunked: weakref.WeakKeyDictionary[sa.Select, tuple[sa.ColumnElement[Any], sa.Select]] = weakref.WeakKeyDictionary()


def execute_in(
    stmt: sa.Select,
    column: sa.ColumnElement[Any],
    values: Iterable[Any],
    params: Params | None = None,
    *,
    using: str = DEFAULT_DB_ALIAS,
    chunk_size: int | None = None,
) -> list[tuple[Any, ...]]:
    """Run ``stmt`` with ``column IN (values)`` added, a chunk of values at a time, and return all rows.

    ``None`` and repeated values are dropped (``IN (NULL)`` matches nothing);
    with no values left nothing runs and the result is ``[]``. The values
    convert as an ``in_()`` list does and the rows as :func:`execute` gives
    them, chunk after chunk in the order of the values' first appearance.
    ``params`` fills the statement's ``bindparam()`` values, one mapping as in
    :func:`execute`, for every chunk; the same statement run again with other
    values or params is not compiled again.

    ``chunk_size`` defaults to the smaller of the connection's
    ``ops.max_in_list_size()`` and ``features.max_query_params`` less the
    statement's own parameters (``params`` included), or 1000 when the database
    states neither.
    Each chunk is a statement of its own, so ``ORDER BY`` and ``LIMIT`` hold
    within a chunk, not across them.
    """
    if not isinstance(stmt, sa.Select):
        raise TypeError(f"execute_in() takes a Select statement, got {type(stmt).__name__}")
    if not isinstance(column, sa.ColumnElement):
        raise TypeError(f"execute_in() takes a column to compare, got {type(column).__name__}")
    _check_chunk_size(chunk_size)
    _check_params(params, "params")
    if params is not None and _VALUES in params:
        raise ValueError(f"params cannot name {_VALUES!r}: execute_in() binds the IN list under that name")
    keys = list(dict.fromkeys(value for value in values if value is not None))
    if not keys:
        return []
    connection = connections[using]
    # One statement for every chunk and call: it compiles once, the chunks only expand its parameter.
    chunked = _chunked_statement(stmt, column)
    own = dict(params or {})
    size = chunk_size or _default_chunk_size(chunked, own, keys[0], connection)
    rows: list[tuple[Any, ...]] = []
    for start in range(0, len(keys), size):
        rows += _run(prepare(chunked, own | {_VALUES: keys[start : start + size]}, connection), connection)[1]
    return rows


def _chunked_statement(stmt: sa.Select, column: sa.ColumnElement[Any]) -> sa.Select:
    try:
        cached = _chunked.get(stmt)
    except TypeError:  # cannot be weakly referenced: built per call
        cached = None
    if cached is not None and cached[0] is column:
        return cached[1]
    chunked = stmt.where(column.in_(sa.bindparam(_VALUES, expanding=True)))
    try:
        _chunked[stmt] = (column, chunked)
    except TypeError:
        pass
    return chunked


def _check_params(params: Any, name: str) -> None:
    if params is not None and not isinstance(params, Mapping):
        raise ValueError(f"{name} must be one mapping of bindparam values or None, got {type(params).__name__}")


def _check_chunk_size(chunk_size: int | None) -> None:
    if chunk_size is not None and (isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0):
        raise ValueError(f"chunk_size must be a positive integer or None, got {chunk_size!r}")


def _default_chunk_size(stmt: sa.Select, params: Params, sample: Any, connection: BaseDatabaseWrapper) -> int:
    limits = []
    if in_list := connection.ops.max_in_list_size():
        limits.append(in_list)
    if most := connection.features.max_query_params:
        # The statement's own parameters, counted as compiled with one value in the list.
        own = len(prepare(stmt, params | {_VALUES: [sample]}, connection).param_rows[0]) - 1
        limits.append(max(most - own, 1))
    return min(limits, default=DEFAULT_IN_CHUNK)


def join_across(
    outer: sa.Select,
    inner: sa.Select,
    on: tuple[Any, Any],
    *,
    params: Params | None = None,
    inner_params: Params | None = None,
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

    ``params`` fills ``outer``'s ``bindparam()`` values and ``inner_params``
    ``inner``'s, each one mapping as in :func:`execute`.
    """
    for stmt, side in ((outer, "outer"), (inner, "inner")):
        if not isinstance(stmt, sa.Select):
            raise TypeError(f"join_across() takes Select statements, got {type(stmt).__name__} as {side}")
    if how not in _HOW:
        raise ValueError(f"how must be 'inner' or 'left', got {how!r}")
    _check_chunk_size(chunk_size)
    _check_params(params, "params")
    _check_params(inner_params, "inner_params")
    outer_key, inner_key = _keys(on)
    outer_at, _ = _find(outer, outer_key, "outer")
    inner_at, inner_column = _find(inner, inner_key, "inner")

    outer_rows = execute(outer, params, using=using)
    inner_rows = execute_in(
        inner,
        inner_column,
        [row[outer_at] for row in outer_rows],
        inner_params,
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
