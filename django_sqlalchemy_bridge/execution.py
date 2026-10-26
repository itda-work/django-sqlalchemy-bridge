"""Compile SQLAlchemy Core statements and run them on Django's own cursor.

SQLAlchemy only builds and compiles the SQL. The statement runs on
``connections[using].cursor()``, so it joins Django's transaction (``atomic()``,
savepoints, ``on_commit``) and shows up in ``execute_wrapper``,
``CaptureQueriesContext`` and ``assertNumQueries``. No SQLAlchemy ``Engine``,
``Connection`` or ``Session`` is involved.

Values convert the way the Django ORM converts them, never through SQLAlchemy's
type processing: a value compared with a column of a :func:`table_for` table
converts as the right-hand side of an ORM lookup (``filter(field=value)``), any
other value bound to such a column as the ORM writes it
(``field.get_db_prep_save()``), and a selected column of such a table goes
through the same converters the ORM applies when reading it. Values with no
column behind them (expressions, literals, ``text()``) pass through raw.

Writes made here bypass the model layer: no ``save()``, no ``pre_save`` /
``post_save`` / ``pre_delete`` / ``post_delete`` signals, no field defaults.
Nor SQLAlchemy's Python-side ``Column(default=...)`` / ``onupdate=...``: a
statement that leaves such a column for them to fill is refused before it runs.
"""

from __future__ import annotations

import contextlib
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from django.db import DEFAULT_DB_ALIAS, DatabaseError, NotSupportedError, connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Expression
from sqlalchemy.engine import BindTyping, Dialect
from sqlalchemy.engine.interfaces import Compiled
from sqlalchemy.sql import operators
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseList, ExpressionClauseList, Grouping

from django_sqlalchemy_bridge import _sa_result
from django_sqlalchemy_bridge.tables import FieldType, _generic_type, _oracle_name

__all__ = ["execute", "execute_pipeline", "execute_result", "stream"]

Params = Mapping[str, Any]


class _ReturningCompiler(SQLCompiler):
    """Note the columns the compiled top-level ``RETURNING`` clause returns.

    The statement's own ``exported_columns`` (and ``effective_returning``, which
    reads the same memo) can be stale: SQLAlchemy memoizes it without
    registering the memo, so a statement derived with ``.returning()`` from one
    whose columns were read keeps the old collection. The compiler is handed
    the columns it renders.
    """

    returned_columns: list[sa.ColumnElement[Any]] | None = None

    def returning_clause(self, stmt: Any, returning_cols: Any, *, populate_result_map: bool, **kw: Any) -> str:
        if populate_result_map:
            columns = [c for e in returning_cols for c in (e.c if isinstance(e, sa.FromClause) else [e])]
            self.returned_columns = [c for c in columns if isinstance(c, sa.ColumnElement)]
        return super().returning_clause(stmt, returning_cols, populate_result_map=populate_result_map, **kw)


class _LookupCompiler(SQLCompiler):
    """Note the bound parameters a comparison takes as its operands: the ORM's lookup values.

    The ORM prepares the value of ``filter(field=value)`` (and ``__lt``,
    ``__in``, ...) otherwise than a value it writes, and a backend may refuse a
    value it would only compare (Django 5.2 on MySQL refuses to write 0 to an
    ``AutoField``). A bind is a lookup value when it is an operand of a
    comparison, or an item of one (a ``BETWEEN`` bound, a tuple's member).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.lookup_binds: list[sa.BindParameter[Any]] = []  # the compiler compiles in __init__
        super().__init__(*args, **kwargs)

    def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
        self.note_lookup_binds(binary)
        return super().visit_binary(binary, override_operator=override_operator, **kw)

    def note_lookup_binds(self, binary: Any) -> None:
        if not operators.is_comparison(binary.operator):
            return
        for operand in (binary.left, binary.right):
            while isinstance(operand, Grouping):
                operand = operand.element
            items = operand.clauses if isinstance(operand, (ClauseList, ExpressionClauseList)) else [operand]
            self.lookup_binds += [item for item in items if isinstance(item, sa.BindParameter)]


def _sqlite_dialect() -> Dialect:
    from sqlalchemy.dialects import sqlite

    dialect = sqlite.dialect(paramstyle="format")

    class Compiler(_LookupCompiler, _ReturningCompiler, dialect.statement_compiler):
        pass

    dialect.statement_compiler = Compiler
    return dialect


def _postgresql_dialect() -> Dialect:
    from sqlalchemy.dialects.postgresql import psycopg

    dialect = psycopg.dialect(paramstyle="format")
    # The psycopg dialect renders casts such as "%s::JSON" for SQLAlchemy's own
    # driver binding; Django binds the values here, and Django's JSONField is
    # jsonb, so the casts would be wrong. Compile plain placeholders, as the ORM does.
    dialect.bind_typing = BindTyping.NONE

    class Compiler(_LookupCompiler, _ReturningCompiler, dialect.statement_compiler):
        def render_literal_value(self, value: Any, type_: sa.types.TypeEngine) -> str:
            # The PostgreSQL compiler doubles backslashes in literals until it has
            # connected and read standard_conforming_strings (SQLAlchemy 2.0 assumes
            # "off"). This dialect never connects, and every PostgreSQL Django
            # supports defaults to "on", so render literals as standard SQL does.
            return SQLCompiler.render_literal_value(self, value, type_)

    dialect.statement_compiler = Compiler
    return dialect


def _mysql_dialect(*, mariadb: bool, backslash_escapes: bool) -> Dialect:
    from sqlalchemy.dialects.mysql import mariadb as sa_mariadb
    from sqlalchemy.dialects.mysql import mysqldb

    # Django's MySQL backend runs on mysqlclient (MySQLdb), a "format" driver.
    factory = sa_mariadb.loader("mysqldb") if mariadb else mysqldb.dialect
    dialect = factory(paramstyle="format")
    # What initialize() would read from the server; the dialect never connects.
    # Every MySQL Django supports (8.0.11+) has FOR UPDATE OF and FOR SHARE.
    dialect.supports_for_update_of = dialect.use_mysql_for_share = not mariadb

    class Compiler(_LookupCompiler, _ReturningCompiler, dialect.statement_compiler):
        if not backslash_escapes:

            def render_literal_value(self, value: Any, type_: sa.types.TypeEngine) -> str:
                # The MySQL compiler doubles backslashes in literals, as the server's
                # default sql_mode wants. Under NO_BACKSLASH_ESCAPES a backslash is
                # an ordinary character, as in standard SQL.
                return SQLCompiler.render_literal_value(self, value, type_)

    dialect.statement_compiler = Compiler
    return dialect


def _is_json(type_: sa.types.TypeEngine) -> bool:
    return isinstance(_generic_type(type_), sa.JSON)


_ERROR_ON_ERROR = " ERROR ON ERROR)"


def _null_on_error(sql: str) -> str:
    return sql[: -len(_ERROR_ON_ERROR)] + ")" if sql.endswith(_ERROR_ON_ERROR) else sql


def _oracle_dialect() -> Dialect:
    from sqlalchemy.dialects.oracle import oracledb

    # Django's Oracle cursor takes "%s" and rewrites it to ":arg0", ":arg1", ...
    dialect = oracledb.dialect(paramstyle="format")

    class Compiler(_LookupCompiler, _ReturningCompiler, dialect.statement_compiler):
        def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
            # Django stores JSONField as text (NCLOB IS JSON), which Oracle cannot
            # compare with "=" (ORA-22848), and a key compiles to JSON_QUERY(),
            # whose text "=" would tell [1,2] from Django's "[1, 2]". Compare JSON
            # values with JSON_EQUAL(), as the ORM compares whole documents.
            if (
                override_operator is None
                and binary.operator in (operators.eq, operators.ne)
                and (_is_json(binary.left.type) or _is_json(binary.right.type))
            ):
                self.note_lookup_binds(binary)
                sql = f"JSON_EQUAL({self.process(binary.left, **kw)}, {self.process(binary.right, **kw)})"
                return sql if binary.operator is operators.eq else f"NOT {sql}"
            return super().visit_binary(binary, override_operator=override_operator, **kw)

        if hasattr(dialect.statement_compiler, "visit_json_getitem_op_binary"):  # SQLAlchemy 2.1+
            # as_string(), as_integer() and the like compile to JSON_VALUE(... ERROR
            # ON ERROR), which fails the whole statement on a row that lacks the key
            # or holds another type. Give NULL there instead, as the other backends do.
            def visit_json_getitem_op_binary(self, binary: Any, operator: Any, **kw: Any) -> str:
                return _null_on_error(super().visit_json_getitem_op_binary(binary, operator, **kw))

            def visit_json_path_getitem_op_binary(self, binary: Any, operator: Any, **kw: Any) -> str:
                return _null_on_error(super().visit_json_path_getitem_op_binary(binary, operator, **kw))

    class Preparer(dialect.preparer):
        def quote(self, ident: str, force: Any = None) -> str:  # force: SQLAlchemy 2.0's deprecated argument
            # Django names every Oracle table, column and alias as quote_name() does:
            # upper case and, past 30 characters, cut with a hash. SQLAlchemy would
            # leave a lower case name unquoted (which Oracle reads upper case, so
            # far the same) but quote a mixed case or long one as it is, naming a
            # column Django never made. Spell them all Django's way, quoted, even a
            # quoted_name(..., quote=True): Django makes no case-sensitive names.
            return self.quote_identifier(_oracle_name(ident))

    dialect.statement_compiler = Compiler
    dialect.preparer = Preparer
    dialect.identifier_preparer = Preparer(dialect)
    return dialect


# Every Django backend here takes "%s" placeholders and "%%" for a literal percent
# sign (the SQLite wrapper rewrites them to "?" and "%") whenever params are passed.
_DIALECT_FACTORIES = {
    "sqlite": _sqlite_dialect,
    "postgresql": _postgresql_dialect,
    "mysql": lambda: _mysql_dialect(mariadb=False, backslash_escapes=True),
    "mysql-no-backslash-escapes": lambda: _mysql_dialect(mariadb=False, backslash_escapes=False),
    "mariadb": lambda: _mysql_dialect(mariadb=True, backslash_escapes=True),
    "mariadb-no-backslash-escapes": lambda: _mysql_dialect(mariadb=True, backslash_escapes=False),
    "oracle": _oracle_dialect,
}
_dialects: dict[str, Dialect] = {}


def _dialect_key(connection: BaseDatabaseWrapper) -> str:
    """Which dialect ``connection`` compiles with: its vendor, told apart further on MySQL.

    Django's "mysql" vendor is MySQL or MariaDB (which differ in RETURNING), and
    the server's sql_mode decides how a literal backslash is written. Asking
    either connects, as running the statement would.
    """
    vendor = connection.vendor
    if vendor != "mysql":
        return vendor
    key = "mariadb" if connection.mysql_is_mariadb else "mysql"
    if "NO_BACKSLASH_ESCAPES" in connection.sql_mode:
        key += "-no-backslash-escapes"
    return key


def dialect_for(connection: BaseDatabaseWrapper) -> Dialect:
    """The SQLAlchemy dialect statements are compiled with for ``connection``."""
    key = _dialect_key(connection)
    dialect = _dialects.get(key)
    if dialect is None:
        try:
            factory = _DIALECT_FACTORIES[key]
        except KeyError:
            raise NotImplementedError(f"database vendor {connection.vendor!r} is not supported") from None
        dialect = _dialects.setdefault(key, factory())
    return dialect


@dataclass
class Prepared:
    """A statement compiled for one connection, with its parameters converted.

    ``param_rows`` holds one positional tuple per execution: exactly one unless
    ``many``. ``converters`` has one entry per result column, ``None`` where the
    value is returned raw; it is empty when the statement's columns are unknown.
    ``rowcount_only`` marks DML that compiled without ``RETURNING``. ``names``
    are the result columns' names as the statement was built (empty when
    unknown), which Oracle reports respelled.
    """

    sql: str
    param_rows: list[tuple[Any, ...]]
    many: bool
    converters: list[tuple[list[Any], Any] | None]
    rowcount_only: bool = False
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Compiled:
    """What compiling a statement for one dialect gives, whatever the connection or values.

    ``types`` and ``names`` are the result columns' types and names (empty when
    the statement's columns are unknown); ``defaults_error`` is why the
    statement is refused, if it is. ``lookups`` names the binds whose values are
    compared with a column (see :class:`_LookupCompiler`).
    """

    compiled: Compiled
    dml: bool
    types: tuple[sa.types.TypeEngine, ...]
    names: tuple[str, ...]
    defaults_error: str | None
    lookups: frozenset[str]


# Statement -> {(dialect key, params' names): _Compiled}. The entry goes when the
# statement object does: what is compiled is a generative copy of it, so nothing
# cached refers back to it. Racing threads may both compile a statement; each
# entry is complete before it is stored, and the last one stored stays.
_cache: weakref.WeakKeyDictionary[sa.sql.ClauseElement, dict[tuple[str, tuple[str, ...] | None], _Compiled]] = (
    weakref.WeakKeyDictionary()
)


def _compile(
    stmt: sa.sql.ClauseElement, column_keys: tuple[str, ...] | None, connection: BaseDatabaseWrapper
) -> _Compiled:
    dialect = dialect_for(connection)
    key = (_dialect_key(connection), column_keys)
    try:
        entries = _cache.get(stmt)
    except TypeError:  # cannot be weakly referenced: not cached
        entries = None
    entry = entries.get(key) if entries is not None else None
    if entry is not None:
        return entry
    if isinstance(stmt, sa.Insert):
        # Otherwise PostgreSQL compiles an implicit "RETURNING id" for SQLAlchemy's
        # inserted_primary_key when no RETURNING is given, and the caller would get
        # rows instead of a rowcount. With an explicit RETURNING it changes nothing.
        target = stmt.inline()
    elif isinstance(stmt, sa.Executable):
        # A copy, so the Compiled (which holds its statement) keeps no hold on stmt.
        target = stmt.execution_options()
    else:
        target = stmt
    compiled = target.compile(dialect=dialect, column_keys=list(column_keys) if column_keys is not None else None)
    dml = isinstance(stmt, sa.sql.dml.UpdateBase)
    columns = compiled.returned_columns if dml else getattr(target, "exported_columns", None)
    columns = list(columns or ())
    names = tuple(name for column in columns if isinstance(name := getattr(column, "name", None), str))
    compared = {id(bind) for bind in compiled.lookup_binds}
    lookups = frozenset(name for name, bind in compiled.binds.items() if id(bind) in compared)
    entry = _Compiled(
        compiled, dml, tuple(column.type for column in columns), names, _python_defaults_error(compiled), lookups
    )
    if target is not stmt:
        try:
            _cache.setdefault(stmt, {})[key] = entry
        except TypeError:
            pass
    return entry


def prepare(
    stmt: sa.sql.ClauseElement,
    params: Params | Sequence[Params] | None,
    connection: BaseDatabaseWrapper,
) -> Prepared:
    """Compile ``stmt`` for ``connection`` and convert ``params`` through the Django fields.

    ``params`` is ``None``, one mapping, or a sequence of mappings (executemany).
    The compilation is reused when the same statement object runs again on the
    same database vendor with params of the same names; the parameters, the
    result converters and the RETURNING check are worked out on every call.
    """
    many = params is not None and not isinstance(params, Mapping)
    rows: list[Params] = list(params) if many else [params or {}]
    entry = _compile(stmt, tuple(rows[0]) if rows else None, connection)
    compiled = entry.compiled
    if many and entry.types:
        raise ValueError("executemany cannot return rows; run a statement with RETURNING once per row")
    if entry.dml and entry.types:
        check_returning(
            connection,
            "UPDATE" if isinstance(stmt, sa.Update) else "DELETE" if isinstance(stmt, sa.Delete) else "INSERT",
        )
    if entry.defaults_error is not None:
        raise ValueError(entry.defaults_error)
    sql = None
    param_rows = []
    for row in rows:
        state = compiled.construct_expanded_state(row, escape_names=False)
        if sql is None:
            sql = state.statement
        elif state.statement != sql:
            raise ValueError("every executemany row must compile to the same SQL (check expanding IN lists)")
        param_rows.append(_positional(compiled, state, entry.lookups, connection))
    converters = [_field_converters(type_, connection) for type_ in entry.types]
    return Prepared(
        sql or compiled.string,
        param_rows,
        many,
        converters,
        rowcount_only=entry.dml and not entry.types,
        names=entry.names,
    )


def check_returning(connection: BaseDatabaseWrapper, verb: str = "INSERT") -> None:
    """Refuse ``verb ... RETURNING`` (``INSERT``, ``UPDATE``, ``DELETE``) before it runs on a database that lacks it.

    The dialects compile without connecting, so they render ``RETURNING`` for
    any database. SQLite has it from 3.35 for INSERT, UPDATE and DELETE alike,
    and Django (5.2 runs on 3.31) reports it as ``can_return_columns_from_insert``.
    MySQL has none; MariaDB, which Django also reports as vendor "mysql", has
    INSERT and DELETE ... RETURNING but no UPDATE ... RETURNING. Oracle returns
    the rows into output bind variables (``RETURNING ... INTO``), which Django's
    cursor does not give back: run anyway, the statement would report its
    rowcount, so it is refused for every verb although Django reports it.
    """
    if connection.vendor == "oracle":
        raise NotSupportedError(
            f"database {connection.alias!r} ({connection.display_name}) returns {verb} ... RETURNING rows into "
            "output binds (RETURNING ... INTO), which the bridge does not support"
        )
    supported = connection.features.can_return_columns_from_insert
    if supported and verb == "UPDATE" and connection.vendor == "mysql":
        supported = False
    if not supported:
        raise NotSupportedError(
            f"database {connection.alias!r} ({connection.display_name}) does not support {verb} ... RETURNING"
        )


def _python_defaults_error(compiled: Compiled) -> str | None:
    # A column left out of an INSERT or UPDATE whose Column(default=...) or
    # Column(onupdate=...) is a Python value or callable is still compiled into
    # the SQL; SQLAlchemy's execution context would fill its bind, Django's
    # cursor binds NULL. SQL expression defaults compile into the statement and
    # never land here.
    prefetch = [*compiled.insert_prefetch, *compiled.update_prefetch]
    if not prefetch:
        return None
    # A multi-VALUES insert adds copies ("<key>_m2", ...) of the first row's columns.
    columns = [column for column in prefetch if isinstance(column, sa.Column)]
    names = ", ".join(dict.fromkeys(f"{column.table.name}.{column.key}" for column in columns))
    return (
        f"SQLAlchemy Python-side default or onupdate of {names} cannot be applied: the bridge runs "
        "on Django's cursor, which would store NULL there; pass the value in the statement or params"
    )


def _positional(
    compiled: Compiled, state: Any, lookups: frozenset[str], connection: BaseDatabaseWrapper
) -> tuple[Any, ...]:
    # Expanded IN parameters ("ids_1", "ids_2") take the type of the one they came from ("ids").
    origin = {name: original for original, names in state.parameter_expansion.items() for name in names}
    values = []
    for name in state.positiontup or ():
        key = origin.get(name, name)
        bind = compiled.binds.get(key)
        type_ = bind.type if bind is not None else None
        values.append(to_db(state.parameters[name], type_, connection, lookup=key in lookups))
    return tuple(values)


def to_db(
    value: Any, type_: sa.types.TypeEngine | None, connection: BaseDatabaseWrapper, *, lookup: bool = False
) -> Any:
    """Convert a value bound against ``type_`` the way the ORM converts it for its field.

    A ``lookup`` value (one compared with a column) converts as the ORM's
    ``filter(field=value)`` does, any other as the ORM writes it. ``None``
    stays SQL ``NULL`` (as ``Model.save()`` stores it, also for
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
    if lookup:
        return lookup_value(type_.field, value, connection)
    return type_.field.get_db_prep_save(value, connection)


def lookup_value(field: Any, value: Any, connection: BaseDatabaseWrapper) -> Any:
    """``value`` as an ORM lookup compares ``field`` with it.

    ``Lookup.get_prep_lookup()`` runs ``get_prep_value()``, then
    ``FieldGetDbPrepValueMixin`` runs ``get_db_prep_value(prepared=True)``, of
    the target field for a relation. Unlike ``get_db_prep_save()`` this checks
    nothing a backend checks only of written values.
    """
    target = getattr(field, "target_field", None) or field
    return target.get_db_prep_value(field.get_prep_value(value), connection, prepared=True)


def _field_converters(type_: sa.types.TypeEngine, connection: BaseDatabaseWrapper) -> tuple[list[Any], Any] | None:
    # The ORM reads a column through Col(field) with the backend's converters
    # first, then the field's (SQLCompiler.get_converters()); do the same.
    if not isinstance(type_, FieldType):
        return None
    field = type_.field
    expression = field.get_col(field.model._meta.db_table)
    functions = connection.ops.get_db_converters(expression)
    if connection.vendor == "oracle":
        # Django's Oracle cursor gives a number with no declared precision (sum(),
        # avg(), CASE) as int or Decimal by its text. The ORM coerces an
        # expression's number to its field's type (BaseExpression.convert_value)
        # before the field's converters; a plain column is typed already.
        coerce = Expression(output_field=field).convert_value
        if coerce is not Expression._convert_value_noop:
            functions.append(coerce)
    functions += expression.get_db_converters(connection)
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


def _run(prepared: Prepared, connection: BaseDatabaseWrapper) -> tuple[list[str] | None, list[tuple] | int]:
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
        keys = _result_keys(cursor.description, prepared.names, connection)
        return keys, from_db(cursor.fetchall(), prepared.converters, connection)


def _result_keys(
    description: Sequence[Sequence[Any]], names: tuple[str, ...], connection: BaseDatabaseWrapper
) -> list[str]:
    """The result's keys: the column names the database reports, read back on Oracle.

    Oracle reports a name as the dialect spelled it, Django's way (upper case,
    cut past 30 characters), so a column or label the statement names keeps the
    name it was built with. Any other name (``text()`` columns, SQLAlchemy's
    anonymous labels such as ``count_1``) is read as SQLAlchemy's Oracle dialect
    reads it: lower case if Oracle reports it all upper case, else as reported.
    """
    keys = [column[0] for column in description]
    if connection.vendor != "oracle":
        return keys
    built = {}
    for name in names:
        built.setdefault(_oracle_name(name), name)
    dialect = dialect_for(connection)
    return [built.get(key) or str(dialect.normalize_name(key)) for key in keys]


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
    connection = connections[using]
    return _run(prepare(stmt, params, connection), connection)[1]


def execute_pipeline(
    items: Iterable[sa.sql.ClauseElement | tuple[sa.sql.ClauseElement, Params | Sequence[Params] | None]],
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> list[list[tuple[Any, ...]] | int]:
    """Run several statements, in one round trip on PostgreSQL, and return each one's result.

    Each item is a statement or a ``(statement, params)`` pair, with the meaning
    and the result :func:`execute` gives it (rows or rowcount; a sequence of
    params runs executemany). The results come in input order. Every statement
    is compiled, and checked (RETURNING, Python-side defaults), before any runs.

    On PostgreSQL with psycopg 3 and a libpq that supports pipeline mode, the
    statements are sent inside ``connection.connection.pipeline()``, each on its
    own Django cursor, and their results read once all are sent. Elsewhere they
    run one after another, with the same results. Either way each statement
    shows up in ``connection.queries`` and ``execute_wrapper``.

    A call that runs more than one statement does so in
    ``atomic(using, savepoint=False)``, so it writes all of its rows or none. A
    failing item raises its own database error, with a note naming its index;
    the items after it do not take effect. Like the ORM, this refuses to run
    from async code (``SynchronousOnlyOperation``).
    """
    connection = connections[using]
    prepared = []
    for index, item in enumerate(items):
        stmt, params = item if isinstance(item, tuple) else (item, None)
        try:
            prepared.append(prepare(stmt, params, connection))
        except Exception as e:
            e.add_note(f"execute_pipeline(): item {index} failed")
            raise
    if not prepared:
        return []
    several = len(prepared) > 1 or prepared[0].many
    with transaction.atomic(using=using, savepoint=False) if several else contextlib.nullcontext():
        if _pipeline_supported(connection):
            return _run_pipeline(prepared, connection)
        results = []
        for index, item in enumerate(prepared):
            try:
                results.append(_run(item, connection)[1])
            except Exception as e:
                e.add_note(f"execute_pipeline(): item {index} failed")
                raise
        return results


def _pipeline_supported(connection: BaseDatabaseWrapper) -> bool:
    if connection.vendor != "postgresql":
        return False
    connection.ensure_connection()
    # psycopg2 has no pipeline; psycopg 3 has one when libpq is 14 or later.
    pipeline = getattr(connection.Database, "Pipeline", None)
    return hasattr(connection.connection, "pipeline") and pipeline is not None and pipeline.is_supported()


def _run_pipeline(prepared: list[Prepared], connection: BaseDatabaseWrapper) -> list[list[tuple] | int]:
    # The statements are all sent before any result is read. psycopg attaches a
    # result to a cursor when it reads it, so after an error the first cursor
    # still without one is the item that failed. An executemany keeps no result
    # (only its rowcount), so a sync follows it: inside the atomic() the caller
    # opened for several statements, a sync commits nothing.
    results: list[list[tuple] | int] = [0] * len(prepared)
    cursors: list[tuple[int, Any]] = []
    synced = 0  # cursors[:synced] have their results
    failed = None
    try:
        with connection.wrap_database_errors, connection.connection.pipeline() as pipeline:
            try:
                for index, item in enumerate(prepared):
                    if item.many and not item.param_rows:
                        continue
                    cursor = connection.cursor()
                    cursors.append((index, cursor))
                    if item.many:
                        cursor.executemany(item.sql, item.param_rows)
                        pipeline.sync()
                        synced = len(cursors)
                    else:
                        # Always pass params, even empty: the backends turn "%%" into "%" only then.
                        cursor.execute(item.sql, item.param_rows[0])
                pipeline.sync()
            except Exception:
                pending = cursors[synced:]
                failed = next(
                    (index for index, cursor in pending if prepared[index].many or cursor.pgresult is None),
                    pending[-1][0] if pending else None,
                )
                raise
            for index, cursor in cursors:
                if cursor.description is None:
                    results[index] = cursor.rowcount
                else:
                    results[index] = from_db(cursor.fetchall(), prepared[index].converters, connection)
    except Exception as e:
        if failed is not None:
            e.add_note(f"execute_pipeline(): item {failed} failed")
        raise
    finally:
        for _, cursor in cursors:
            cursor.close()
    return results


def _prepare_one(stmt: sa.sql.ClauseElement, params: Any, connection: BaseDatabaseWrapper, caller: str) -> Prepared:
    if params is not None and not isinstance(params, Mapping):
        raise TypeError(f"{caller}() runs one statement; pass a single mapping of params")
    prepared = prepare(stmt, params, connection)
    if prepared.rowcount_only:
        raise ValueError("statement returns no rows; use execute() for its rowcount")
    return prepared


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
    connection = connections[using]
    keys, rows = _run(_prepare_one(stmt, params, connection, "execute_result"), connection)
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
    connection = connections[using]
    prepared = _prepare_one(stmt, params, connection, "stream")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    # PostgreSQL declares a server-side cursor only for SELECT or VALUES, so DML
    # with RETURNING takes a plain cursor (the driver then holds all its rows).
    chunked = not isinstance(stmt, sa.sql.dml.UpdateBase)
    return _stream(connection, prepared, chunk_size, chunked)


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
