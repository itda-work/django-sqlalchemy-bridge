"""Compile SQLAlchemy Core statements and run them on Django's own cursor.

SQLAlchemy only builds and compiles the SQL. The statement runs on
``connections[using].cursor()``, so it joins Django's transaction (``atomic()``,
savepoints, ``on_commit``) and shows up in ``execute_wrapper``,
``CaptureQueriesContext`` and ``assertNumQueries``. No SQLAlchemy ``Engine``,
``Connection`` or ``Session`` is involved.

Values convert the way the Django ORM converts them, never through SQLAlchemy's
type processing: a value bound to a column of a :func:`table_for` table goes
through ``field.get_db_prep_value()``, and a selected column of such a table goes
through the same converters the ORM applies when reading it. Values with no
column behind them (expressions, literals, ``text()``) pass through raw.

Writes made here bypass the model layer: no ``save()``, no ``pre_save`` /
``post_save`` / ``pre_delete`` / ``post_delete`` signals, no field defaults.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from django.db import DEFAULT_DB_ALIAS, DatabaseError, NotSupportedError, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from sqlalchemy.engine import BindTyping, Dialect
from sqlalchemy.engine.interfaces import Compiled
from sqlalchemy.sql.compiler import SQLCompiler

from django_sqlalchemy_bridge import _sa_result
from django_sqlalchemy_bridge.tables import FieldType

__all__ = ["execute", "execute_result", "stream"]

Params = Mapping[str, Any]


def _sqlite_dialect() -> Dialect:
    from sqlalchemy.dialects import sqlite

    return sqlite.dialect(paramstyle="format")


def _postgresql_dialect() -> Dialect:
    from sqlalchemy.dialects.postgresql import psycopg

    dialect = psycopg.dialect(paramstyle="format")
    # The psycopg dialect renders casts such as "%s::JSON" for SQLAlchemy's own
    # driver binding; Django binds the values here, and Django's JSONField is
    # jsonb, so the casts would be wrong. Compile plain placeholders, as the ORM does.
    dialect.bind_typing = BindTyping.NONE

    class Compiler(dialect.statement_compiler):
        def render_literal_value(self, value: Any, type_: sa.types.TypeEngine) -> str:
            # The PostgreSQL compiler doubles backslashes in literals until it has
            # connected and read standard_conforming_strings (SQLAlchemy 2.0 assumes
            # "off"). This dialect never connects, and every PostgreSQL Django
            # supports defaults to "on", so render literals as standard SQL does.
            return SQLCompiler.render_literal_value(self, value, type_)

    dialect.statement_compiler = Compiler
    return dialect


# Both Django backends take "%s" placeholders and "%%" for a literal percent sign
# (the SQLite wrapper rewrites them to "?" and "%") whenever params are passed.
_DIALECT_FACTORIES = {"sqlite": _sqlite_dialect, "postgresql": _postgresql_dialect}
_dialects: dict[str, Dialect] = {}


def dialect_for(connection: BaseDatabaseWrapper) -> Dialect:
    """The SQLAlchemy dialect statements are compiled with for ``connection``."""
    vendor = connection.vendor
    dialect = _dialects.get(vendor)
    if dialect is None:
        try:
            factory = _DIALECT_FACTORIES[vendor]
        except KeyError:
            raise NotImplementedError(f"database vendor {vendor!r} is not supported") from None
        dialect = _dialects.setdefault(vendor, factory())
    return dialect


@dataclass
class Prepared:
    """A statement compiled for one connection, with its parameters converted.

    ``param_rows`` holds one positional tuple per execution: exactly one unless
    ``many``. ``converters`` has one entry per result column, ``None`` where the
    value is returned raw; it is empty when the statement's columns are unknown.
    """

    sql: str
    param_rows: list[tuple[Any, ...]]
    many: bool
    converters: list[tuple[list[Any], Any] | None]


def prepare(
    stmt: sa.sql.ClauseElement,
    params: Params | Sequence[Params] | None,
    connection: BaseDatabaseWrapper,
) -> Prepared:
    """Compile ``stmt`` for ``connection`` and convert ``params`` through the Django fields.

    ``params`` is ``None``, one mapping, or a sequence of mappings (executemany).
    """
    many = params is not None and not isinstance(params, Mapping)
    rows: list[Params] = list(params) if many else [params or {}]
    if many and _returns_rows(stmt):
        raise ValueError("executemany cannot return rows; run a statement with RETURNING once per row")
    if isinstance(stmt, sa.sql.dml.UpdateBase) and _returns_rows(stmt):
        check_returning(connection)
    if isinstance(stmt, sa.Insert) and not _returns_rows(stmt):
        # Otherwise PostgreSQL compiles an implicit "RETURNING id" for SQLAlchemy's
        # inserted_primary_key, and the caller would get rows instead of a rowcount.
        stmt = stmt.inline()
    compiled = stmt.compile(dialect=dialect_for(connection), column_keys=list(rows[0]) if rows else None)
    sql = None
    param_rows = []
    for row in rows:
        state = compiled.construct_expanded_state(row, escape_names=False)
        if sql is None:
            sql = state.statement
        elif state.statement != sql:
            raise ValueError("every executemany row must compile to the same SQL (check expanding IN lists)")
        param_rows.append(_positional(compiled, state, connection))
    return Prepared(sql or compiled.string, param_rows, many, _converters(stmt, connection))


def check_returning(connection: BaseDatabaseWrapper) -> None:
    """Refuse ``RETURNING`` before it runs on a database that lacks it.

    The dialects compile without connecting, so they render ``RETURNING`` for
    any SQLite; it arrived in SQLite 3.35 for INSERT, UPDATE and DELETE alike,
    and Django (5.2 runs on 3.31) reports it as ``can_return_columns_from_insert``.
    """
    if not connection.features.can_return_columns_from_insert:
        raise NotSupportedError(f"database {connection.alias!r} ({connection.display_name}) does not support RETURNING")


def _returns_rows(stmt: sa.sql.ClauseElement) -> bool:
    return bool(getattr(stmt, "exported_columns", ()))


def _positional(compiled: Compiled, state: Any, connection: BaseDatabaseWrapper) -> tuple[Any, ...]:
    # Expanded IN parameters ("ids_1", "ids_2") take the type of the one they came from ("ids").
    origin = {name: original for original, names in state.parameter_expansion.items() for name in names}
    values = []
    for name in state.positiontup or ():
        bind = compiled.binds.get(origin.get(name, name))
        values.append(to_db(state.parameters[name], bind.type if bind is not None else None, connection))
    return tuple(values)


def to_db(value: Any, type_: sa.types.TypeEngine | None, connection: BaseDatabaseWrapper) -> Any:
    """Convert a value bound against ``type_`` the way Django writes its field.

    ``None`` stays SQL ``NULL`` (as ``Model.save()`` stores it, also for
    ``JSONField``); values with no Django field behind them pass through,
    except JSON keys and paths (``column["k"]``), which the dialect spells.
    """
    if isinstance(type_, (sa.JSON.JSONIndexType, sa.JSON.JSONPathType)):
        # A key or path is syntax, not data: SQLite wants '$."k"', PostgreSQL '{a, b}'.
        dialect = dialect_for(connection)
        process = type_.dialect_impl(dialect).bind_processor(dialect)
        return process(value) if process is not None else value
    if value is None or not isinstance(type_, FieldType):
        return value
    return type_.field.get_db_prep_value(value, connection, prepared=False)


def _converters(stmt: sa.sql.ClauseElement, connection: BaseDatabaseWrapper) -> list[tuple[list[Any], Any] | None]:
    columns = getattr(stmt, "exported_columns", None)
    if not columns:
        return []
    return [_field_converters(column.type, connection) for column in columns]


def _field_converters(type_: sa.types.TypeEngine, connection: BaseDatabaseWrapper) -> tuple[list[Any], Any] | None:
    # The ORM reads a column through Col(field) with the backend's converters
    # first, then the field's (SQLCompiler.get_converters()); do the same.
    if not isinstance(type_, FieldType):
        return None
    field = type_.field
    expression = field.get_col(field.model._meta.db_table)
    functions = connection.ops.get_db_converters(expression) + expression.get_db_converters(connection)
    return (functions, expression) if functions else None


def from_db(
    rows: Sequence[Sequence[Any]],
    converters: list[tuple[list[Any], Any] | None],
    connection: BaseDatabaseWrapper,
) -> list[tuple[Any, ...]]:
    """Convert fetched rows with the converters :func:`prepare` found."""
    active = [(i, c) for i, c in enumerate(converters) if c is not None]
    if not active or (rows and len(rows[0]) != len(converters)):
        return [tuple(row) for row in rows]
    out = []
    for row in rows:
        row = list(row)
        for i, (functions, expression) in active:
            value = row[i]
            for function in functions:
                value = function(value, expression, connection)
            row[i] = value
        out.append(tuple(row))
    return out


def _run(stmt: sa.sql.ClauseElement, params: Any, using: str) -> tuple[list[str] | None, list[tuple] | int]:
    connection = connections[using]
    prepared = prepare(stmt, params, connection)
    if prepared.many and not prepared.param_rows:
        return None, 0
    with connection.cursor() as cursor:
        if prepared.many:
            cursor.executemany(prepared.sql, prepared.param_rows)
        else:
            # Always pass params, even empty: the backends turn "%%" into "%" only then.
            cursor.execute(prepared.sql, prepared.param_rows[0])
        if cursor.description is None:
            return None, cursor.rowcount
        keys = [column[0] for column in cursor.description]
        return keys, from_db(cursor.fetchall(), prepared.converters, connection)


def execute(
    stmt: sa.sql.ClauseElement,
    params: Params | Sequence[Params] | None = None,
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> list[tuple[Any, ...]] | int:
    """Run ``stmt`` on Django database ``using`` and return its rows or rowcount.

    A statement that returns rows (``SELECT``, ``RETURNING``) gives a list of
    tuples; any other gives the cursor's rowcount. ``params`` fills
    ``bindparam()`` values (and names the columns of a bare ``insert(table)``);
    a sequence of mappings runs the statement once per mapping with
    ``cursor.executemany()``.

    The statement runs inside whatever Django transaction is open on that
    connection. Writes made this way send no model signals (``pre_save``,
    ``post_save``, ...) and skip ``save()``; see the module docstring for how
    values convert. Like the ORM, this refuses to run from async code
    (``SynchronousOnlyOperation``).
    """
    return _run(stmt, params, using)[1]


def _check_returns_rows(stmt: sa.sql.ClauseElement, params: Any, caller: str) -> None:
    if isinstance(stmt, sa.sql.dml.UpdateBase) and not _returns_rows(stmt):
        raise ValueError("statement returns no rows; use execute() for its rowcount")
    if params is not None and not isinstance(params, Mapping):
        raise TypeError(f"{caller}() runs one statement; pass a single mapping of params")


def execute_result(
    stmt: sa.sql.ClauseElement,
    params: Params | None = None,
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> sa.Result:
    """Like :func:`execute`, but return the rows as a SQLAlchemy ``Result``.

    The rows are fetched and converted before this returns; the ``Result``
    offers ``row.name``, ``row._mapping``, ``.mappings()``, ``.scalars()`` and
    the rest over them. Only statements that return rows are accepted: DML
    without ``RETURNING`` raises ``ValueError`` before running, and a ``text()``
    statement that turns out to return none raises after it ran.
    """
    _check_returns_rows(stmt, params, "execute_result")
    keys, rows = _run(stmt, params, using)
    if keys is None:
        raise ValueError("statement returned no rows; use execute() for its rowcount")
    return _sa_result.result_from_rows(keys, rows)


# QuerySet.iterator()'s default chunk size.
DEFAULT_CHUNK_SIZE = 2000


def stream(
    stmt: sa.sql.ClauseElement,
    params: Params | None = None,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    using: str = DEFAULT_DB_ALIAS,
) -> Iterator[tuple[Any, ...]]:
    """Like :func:`execute` for a statement that returns rows, but yield them a chunk at a time.

    Rows are fetched ``chunk_size`` at a time and convert as in :func:`execute`.
    The cursor is the one ``QuerySet.iterator()`` takes: ``chunked_cursor()``,
    a server-side cursor on PostgreSQL, unless the database sets
    ``DISABLE_SERVER_SIDE_CURSORS``. Nothing runs until the first row is asked
    for; the cursor closes when the rows run out, the generator is closed, or
    an error ends it.

    DML with ``RETURNING`` runs on a plain cursor, since PostgreSQL declares
    server-side cursors only for ``SELECT`` and ``VALUES``; a ``text()``
    statement there must be one of those too. DML without ``RETURNING`` and a
    sequence of params are refused before anything runs; a ``text()``
    statement that returns no rows raises ``ValueError`` on the first ``next()``.
    """
    _check_returns_rows(stmt, params, "stream")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    connection = connections[using]
    # PostgreSQL declares a server-side cursor only for SELECT or VALUES, so DML
    # with RETURNING takes a plain cursor (the driver then holds all its rows).
    chunked = not isinstance(stmt, sa.sql.dml.UpdateBase)
    return _stream(connection, prepare(stmt, params, connection), chunk_size, chunked)


def _stream(
    connection: BaseDatabaseWrapper, prepared: Prepared, chunk_size: int, chunked: bool
) -> Iterator[tuple[Any, ...]]:
    # The same steps as SQLCompiler.execute_sql(chunked_fetch=True) and cursor_iter().
    if chunked and not connection.settings_dict.get("DISABLE_SERVER_SIDE_CURSORS"):
        cursor = connection.chunked_cursor()
    else:
        cursor = connection.cursor()
    try:
        cursor.execute(prepared.sql, prepared.param_rows[0])
    except Exception as e:
        # Closing a server-side cursor can fail too (the transaction is aborted).
        try:
            cursor.close()
        except DatabaseError:
            raise e from None
        raise
    try:
        if cursor.description is None:
            raise ValueError("statement returned no rows; use execute() for its rowcount")
        while rows := cursor.fetchmany(chunk_size):
            yield from from_db(rows, prepared.converters, connection)
    finally:
        cursor.close()
