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
from django.db.models import Expression, Model
from django.db.models.query import RawQuerySet
from sqlalchemy.engine import BindTyping, Dialect
from sqlalchemy.engine.interfaces import Compiled
from sqlalchemy.sql import operators
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseList, ExpressionClauseList, Grouping

from django_sqlalchemy_bridge import _sa_result
from django_sqlalchemy_bridge.tables import FieldType, _db_name, _generic_type, _oracle_name

__all__ = ["copy_to", "execute", "execute_pipeline", "execute_result", "raw", "stream"]

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
            # A row of an IN list of tuple_()s is a tuple in the list.
            items = [
                i for item in items for i in (item.clauses if isinstance(item, sa.sql.expression.Tuple) else [item])
            ]
            self.lookup_binds += [item for item in items if isinstance(item, sa.BindParameter)]


class _TupleValuesCompiler(SQLCompiler):
    """Bind the values of a ``tuple_()`` compared with a tuple of columns with the columns' types.

    ``tuple_(a, b) == (x, y)`` and ``.in_([(x, y)])`` bind each value with its
    column's type, so it converts as an ORM lookup on that column. Wrapped in
    ``tuple_()`` (``tuple_(a, b) == tuple_(x, y)``, ``.in_([tuple_(x, y)])``) each
    value is a bind of its own, typed by what SQLAlchemy guesses from the value,
    and would go to the driver unconverted: on SQLite an aware datetime would
    match nothing, a dict fail. Such a comparison is compiled with those binds
    rebound with the columns' types; a list of rows of plain values becomes the
    plain tuples' list. A value that is not a bind (a column, an expression) and
    a member with no Django field (``sa.literal()``) are left as they are.
    """

    def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
        if override_operator is None and operators.is_comparison(binary.operator):
            rewritten = _typed_tuple_values(binary)
            if rewritten is not None:
                return self.process(rewritten, **kw)
        return super().visit_binary(binary, override_operator=override_operator, **kw)


def _typed_tuple_values(binary: Any) -> sa.ColumnElement[bool] | None:
    """``binary`` with its ``tuple_()`` values bound with the left tuple's types, or ``None`` if none needs it."""
    left, op, right = binary.left, binary.operator, binary.right
    if not isinstance(left, sa.sql.expression.Tuple):
        return None
    types = [member.type for member in left.clauses]
    if not any(isinstance(type_, FieldType) for type_ in types):
        return None

    def retyped(row: Any) -> list[Any] | None:
        """The row's values rebound, or ``None`` if it has other than binds or none to rebind."""
        values = list(row.clauses)
        if len(values) != len(types) or not all(isinstance(v, sa.BindParameter) and not v.expanding for v in values):
            return None
        rebound = [_retyped_bind(value, type_) for value, type_ in zip(values, types, strict=True)]
        return None if all(new is old for new, old in zip(rebound, values, strict=True)) else rebound

    if isinstance(right, sa.sql.expression.Tuple):
        values = retyped(right)
        return None if values is None else op(left, sa.tuple_(*values))
    if op not in (operators.in_op, operators.not_in_op) or not isinstance(right, Grouping):
        return None
    rows = right.element.clauses if isinstance(right.element, ClauseList) else ()
    if not rows or not all(isinstance(row, sa.sql.expression.Tuple) for row in rows):
        return None
    if all(
        isinstance(v, sa.BindParameter) and v.unique and v.callable is None and not v.expanding
        for row in rows
        for v in row.clauses
    ) and all(len(row.clauses) == len(types) for row in rows):
        # Literal values only: the plain tuples' list, one expanding bind typed by the columns.
        return op(left, [tuple(v.value for v in row.clauses) for row in rows])
    typed = [retyped(row) for row in rows]
    if all(values is None for values in typed):
        return None
    return op(left, [sa.tuple_(*(values or row.clauses)) for values, row in zip(typed, rows, strict=True)])


def _retyped_bind(bind: sa.BindParameter[Any], type_: sa.types.TypeEngine) -> sa.BindParameter[Any]:
    if isinstance(bind.type, FieldType) or not isinstance(type_, FieldType):
        return bind
    if bind.unique:
        return sa.bindparam(None, bind.value, type_=type_, unique=True, callable_=bind.callable)
    return sa.bindparam(bind.key, bind.value, type_=type_, required=bind.required, callable_=bind.callable)


def _is_decimal(type_: sa.types.TypeEngine) -> bool:
    return isinstance(type_, FieldType) and type_.field.get_internal_type() == "DecimalField"


# The compile keyword a window, FILTER or WITHIN GROUP passes its element (in a
# list it empties) so the element leaves the cast to it.
_CAST_OUTSIDE = "dsb_cast_outside"


class _SQLiteNumericCompiler(SQLCompiler):
    """Cast an expression of a ``DecimalField``'s type to ``NUMERIC``, as the ORM does on SQLite.

    Django binds a ``DecimalField`` value as text on SQLite. A column has NUMERIC
    affinity and compares with text as a number, but the result of ``SUM()``,
    ``a + b`` or ``CASE`` has none, and there a number is less than any text:
    ``SUM(price) >= '51'`` is false on every row. The ORM wraps every function,
    arithmetic, ``CASE`` and window whose output field is a ``DecimalField`` in
    ``(CAST(... AS NUMERIC))`` (``SQLiteNumericMixin``), which has the affinity;
    so does this, by the expression's type. A function under a window or
    ``FILTER`` is cast outside them, where the ORM casts it.

    Only the element right under the one being compiled is left uncast: the
    window passes it down in the compile keywords, and the element takes it off
    as it is compiled. The same object anywhere else in the statement (in
    ``HAVING``, beside it in the columns, in the window's ``ORDER BY``) is cast
    as the ORM casts it there (#72).
    """

    def outside_cast(self, element: Any, kw: dict[str, Any]) -> bool:
        """Whether a window or ``FILTER`` casts this element; takes the mark off ``kw`` either way."""
        under = kw.pop(_CAST_OUTSIDE, None)
        if under and under[0] is element:
            under.clear()  # once: the window's PARTITION BY or ORDER BY may hold the same object
            return True
        return False

    def cast_numeric(self, element: Any, sql: str, outside: bool = False) -> str:
        if _is_decimal(element.type) and not outside:
            return f"(CAST({sql} AS NUMERIC))"
        return sql

    def visit_function(self, func: Any, **kw: Any) -> str:
        outside = self.outside_cast(func, kw)
        return self.cast_numeric(func, super().visit_function(func, **kw), outside)

    def visit_funcfilter(self, funcfilter: Any, **kw: Any) -> str:
        outside = self.outside_cast(funcfilter, kw)
        sql = super().visit_funcfilter(funcfilter, **kw, **{_CAST_OUTSIDE: [funcfilter.func]})
        return self.cast_numeric(funcfilter, sql, outside)

    def visit_over(self, over: Any, **kw: Any) -> str:
        outside = self.outside_cast(over, kw)
        return self.cast_numeric(over, super().visit_over(over, **kw, **{_CAST_OUTSIDE: [over.element]}), outside)

    def visit_withingroup(self, withingroup: Any, **kw: Any) -> str:
        outside = self.outside_cast(withingroup, kw)
        sql = super().visit_withingroup(withingroup, **kw, **{_CAST_OUTSIDE: [withingroup.element]})
        return self.cast_numeric(withingroup, sql, outside)

    def visit_case(self, clause: Any, **kw: Any) -> str:
        outside = self.outside_cast(clause, kw)
        return self.cast_numeric(clause, super().visit_case(clause, **kw), outside)

    def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
        outside = self.outside_cast(binary, kw)
        # A comparison is a boolean; this is arithmetic.
        sql = super().visit_binary(binary, override_operator=override_operator, **kw)
        return self.cast_numeric(binary, sql, outside)

    def visit_unary(self, unary: Any, **kw: Any) -> str:
        outside = self.outside_cast(unary, kw)
        sql = super().visit_unary(unary, **kw)
        # Not a modifier (DESC, NULLS FIRST) nor DISTINCT, which keep their operand's type.
        return self.cast_numeric(unary, sql, outside) if unary.operator is operators.neg else sql


def _sqlite_dialect() -> Dialect:
    from sqlalchemy.dialects import sqlite

    dialect = sqlite.dialect(paramstyle="format")

    class Compiler(
        _TupleValuesCompiler, _SQLiteNumericCompiler, _LookupCompiler, _ReturningCompiler, dialect.statement_compiler
    ):
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

    class Compiler(_TupleValuesCompiler, _LookupCompiler, _ReturningCompiler, dialect.statement_compiler):
        def render_literal_value(self, value: Any, type_: sa.types.TypeEngine) -> str:
            # The PostgreSQL compiler doubles backslashes in literals until it has
            # connected and read standard_conforming_strings (SQLAlchemy 2.0 assumes
            # "off"). This dialect never connects, and every PostgreSQL Django
            # supports defaults to "on", so render literals as standard SQL does.
            return SQLCompiler.render_literal_value(self, value, type_)

    dialect.statement_compiler = Compiler
    return dialect


def _is_json(type_: sa.types.TypeEngine) -> bool:
    return isinstance(_generic_type(type_), sa.JSON)


class _JSONTupleCompiler(SQLCompiler):
    """Compare a tuple with a JSON member member by member, as a lone JSON comparison compiles.

    SQLAlchemy renders ``tuple_(a, meta).in_([...])`` as one expanding bind and
    applies no ``bind_expression`` to its members, so on MySQL the value lacks
    ``JSON_EXTRACT(%s, '$')`` and the document compares as text; Oracle
    compares the ``NCLOB`` with ``=`` (ORA-22848). A literal list is written as
    the ``OR`` of each row's ``AND`` of member comparisons, an empty one as
    ``1 != 1``; so is a list of ``tuple_()`` rows. An expanding ``bindparam``'s rows come only at run time, so it is
    refused before anything runs. ``json_tuple_operators`` are the operators
    rewritten: Oracle adds ``==`` and ``!=`` with a tuple of values.
    """

    json_tuple_operators: frozenset[Any] = frozenset({operators.in_op, operators.not_in_op})

    def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
        if override_operator is None and binary.operator in self.json_tuple_operators:
            rewritten = _json_tuple_comparison(binary, self.dialect.name)
            if rewritten is not None:
                return self.process(rewritten, **kw)
        return super().visit_binary(binary, override_operator=override_operator, **kw)


def _json_tuple_comparison(binary: Any, dialect_name: str) -> sa.ColumnElement[bool] | None:
    """``binary`` member by member if it compares a ``tuple_()`` with a JSON member, else ``None``."""
    left, op, right = binary.left, binary.operator, binary.right
    if not (isinstance(left, sa.sql.expression.Tuple) and any(_is_json(c.type) for c in left.clauses)):
        return None
    members = list(left.clauses)
    if op in (operators.eq, operators.ne):
        if not (isinstance(right, sa.sql.expression.Tuple) and len(right.clauses) == len(members)):
            return None
        both = sa.and_(*(member == value for member, value in zip(members, right.clauses, strict=True)))
        return both.self_group() if op is operators.eq else sa.not_(both.self_group())
    if isinstance(right, Grouping) and isinstance(right.element, ClauseList):
        # in_([tuple_(...), ...]) whose rows hold named binds or expressions (see _typed_tuple_values).
        rows = [row.clauses for row in right.element.clauses if isinstance(row, sa.sql.expression.Tuple)]
        if len(rows) != len(right.element.clauses) or any(len(row) != len(members) for row in rows):
            return None
        values = [list(row) for row in rows]
    elif isinstance(right, sa.BindParameter) and right.expanding:
        # in_([...]) makes an anonymous (unique) expanding bind holding the rows.
        _refuse_expanding(right, dialect_name, "a tuple with a JSON member", "tuple_(...).in_([(x, y), ...])")
        types = right.type.types
        values = [
            [sa.bindparam(None, value, type_=types[i], unique=True) for i, value in enumerate(row)]
            for row in right.value
        ]
    else:
        return None  # a subquery compares columns with columns
    rows = [
        sa.and_(*(member == value for member, value in zip(members, row, strict=True))).self_group() for row in values
    ]
    return _either(rows, op)


def _is_literal_list(bind: sa.BindParameter[Any]) -> bool:
    """Whether an expanding bind is the anonymous one ``in_([...])`` makes, its values known when compiling."""
    return bind.unique and bind.callable is None and bind.value is not None


def _refuse_expanding(bind: sa.BindParameter[Any], dialect_name: str, what: str, literal: str) -> None:
    """Refuse an expanding ``bindparam`` given by name, whose values are bound only at run time."""
    if not _is_literal_list(bind):
        raise NotSupportedError(
            f"{dialect_name} cannot compare {what} IN an expanding bindparam: its values are bound at run "
            f"time, where the JSON value would not compare as a document. Pass a literal list ({literal}) "
            "or compare the JSON column in a condition of its own."
        )


def _either(comparisons: list[sa.ColumnElement[bool]], op: Any) -> sa.ColumnElement[bool]:
    """The ``OR`` of ``comparisons`` for ``in_op``, its negation for ``not_in_op``."""
    if not comparisons:
        # SQLAlchemy's empty set still compares with NULLs, which Oracle refuses for the NCLOB.
        return sa.literal_column("1 != 1" if op is operators.in_op else "1 = 1")
    either = sa.or_(*comparisons).self_group()
    return either if op is operators.in_op else sa.not_(either)


def _json_in_comparison(binary: Any, dialect_name: str) -> sa.ColumnElement[bool] | None:
    """``binary`` as an ``OR`` of ``==`` if it compares a JSON document ``IN`` a list, else ``None``.

    A key or path (``meta["k"]``) is not a document: its ``IN`` is left as SQLAlchemy writes it.
    """
    left, right = binary.left, binary.right
    if not _is_json(left.type) or (
        isinstance(left, sa.BinaryExpression)
        and left.operator in (operators.json_getitem_op, operators.json_path_getitem_op)
    ):
        return None
    if not (isinstance(right, sa.BindParameter) and right.expanding):
        return None  # a subquery
    _refuse_expanding(right, dialect_name, "a JSON column", "meta.in_([{...}, ...])")
    return _either(
        [left == sa.bindparam(None, value, type_=right.type, unique=True) for value in right.value], binary.operator
    )


def _wraps_each_value(type_: sa.types.TypeEngine, dialect: Dialect) -> bool:
    """Whether ``type_`` writes each bound value inside its ``bind_expression()`` (MySQL's JSON, #37)."""
    return type_.dialect_impl(dialect).bind_expression(sa.bindparam(None, type_=type_)) is not None


def _mysql_dialect(*, mariadb: bool, backslash_escapes: bool) -> Dialect:
    from sqlalchemy.dialects.mysql import mariadb as sa_mariadb
    from sqlalchemy.dialects.mysql import mysqldb

    # Django's MySQL backend runs on mysqlclient (MySQLdb), a "format" driver.
    factory = sa_mariadb.loader("mysqldb") if mariadb else mysqldb.dialect
    dialect = factory(paramstyle="format")
    # What initialize() would read from the server; the dialect never connects.
    # Every MySQL Django supports (8.0.11+) has FOR UPDATE OF and FOR SHARE.
    dialect.supports_for_update_of = dialect.use_mysql_for_share = not mariadb

    class Compiler(
        _TupleValuesCompiler, _JSONTupleCompiler, _LookupCompiler, _ReturningCompiler, dialect.statement_compiler
    ):
        def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
            # SQLAlchemy writes an empty list's "NULL) AND (1 != 1" inside each value's
            # bind_expression(): meta IN (JSON_EXTRACT(NULL) AND (1 != 1, '$')), which
            # MySQL refuses. A literal one is known now; an expanding bindparam's
            # values come at run time, and prepare() refuses an empty list for it.
            right = binary.right
            if (
                override_operator is None
                and binary.operator in (operators.in_op, operators.not_in_op)
                and isinstance(right, sa.BindParameter)
                and right.expanding
                and _is_literal_list(right)
                and not right.value
                and _wraps_each_value(right.type, self.dialect)
            ):
                return self.process(_either([], binary.operator), **kw)
            return super().visit_binary(binary, override_operator=override_operator, **kw)

        if not backslash_escapes:

            def render_literal_value(self, value: Any, type_: sa.types.TypeEngine) -> str:
                # The MySQL compiler doubles backslashes in literals, as the server's
                # default sql_mode wants. Under NO_BACKSLASH_ESCAPES a backslash is
                # an ordinary character, as in standard SQL.
                return SQLCompiler.render_literal_value(self, value, type_)

    dialect.statement_compiler = Compiler
    return dialect


_ERROR_ON_ERROR = " ERROR ON ERROR)"


def _null_on_error(sql: str) -> str:
    return sql[: -len(_ERROR_ON_ERROR)] + ")" if sql.endswith(_ERROR_ON_ERROR) else sql


def _oracle_dialect() -> Dialect:
    from sqlalchemy.dialects.oracle import oracledb

    # Django's Oracle cursor takes "%s" and rewrites it to ":arg0", ":arg1", ...
    dialect = oracledb.dialect(paramstyle="format")

    class Compiler(
        _TupleValuesCompiler, _JSONTupleCompiler, _LookupCompiler, _ReturningCompiler, dialect.statement_compiler
    ):
        json_tuple_operators = _JSONTupleCompiler.json_tuple_operators | {operators.eq, operators.ne}

        def visit_binary(self, binary: Any, override_operator: Any = None, **kw: Any) -> str:
            # Django stores JSONField as text (NCLOB IS JSON), which Oracle cannot
            # compare with "=" (ORA-22848), and a key compiles to JSON_QUERY(),
            # whose text "=" would tell [1,2] from Django's "[1, 2]". Compare JSON
            # values with JSON_EQUAL(), as the ORM compares whole documents.
            if override_operator is None and binary.operator in (operators.in_op, operators.not_in_op):
                # meta.in_([...]) would compare the NCLOB with "=" too.
                rewritten = _json_in_comparison(binary, self.dialect.name)
                if rewritten is not None:
                    return self.process(rewritten, **kw)
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
    converters: list[tuple[tuple[Any, ...], Any] | None]
    rowcount_only: bool = False
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Compiled:
    """What compiling a statement for one dialect gives, whatever the connection or values.

    ``types`` and ``names`` are the result columns' types and names (empty when
    the statement's columns are unknown), and ``direct`` says which of them are
    table columns rather than expressions (see :func:`_is_column`); ``defaults_error`` is why the
    statement is refused, if it is. ``lookups`` names the binds whose values are
    compared with a column (see :class:`_LookupCompiler`). ``wrapped_lists`` names the
    MySQL expanding binds whose values each go inside their type's ``bind_expression()``,
    which cannot take an empty list (see :func:`_refuse_empty_wrapped_lists`).
    """

    compiled: Compiled
    dml: bool
    types: tuple[sa.types.TypeEngine, ...]
    direct: tuple[bool, ...]
    names: tuple[str, ...]
    defaults_error: str | None
    lookups: frozenset[str]
    wrapped_lists: frozenset[str]


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
    wrapped_lists = frozenset(
        name
        for name, bind in compiled.binds.items()
        if dialect.name in ("mysql", "mariadb") and bind.expanding and _wraps_each_value(bind.type, dialect)
    )
    entry = _Compiled(
        compiled,
        dml,
        tuple(column.type for column in columns),
        tuple(_is_column(column) for column in columns),
        names,
        _python_defaults_error(compiled),
        lookups,
        wrapped_lists,
    )
    if target is not stmt:
        try:
            _cache.setdefault(stmt, {})[key] = entry
        except TypeError:
            pass
    return entry


def _is_column(column: sa.ColumnElement) -> bool:
    """Whether a result column reads a table column (the ORM's ``Col``) rather than an expression.

    A column stays one through labels, subqueries, CTEs and the first SELECT
    of a UNION (the ORM reads a union by its first query's columns too): its
    base columns are the table's. An aggregate, arithmetic, ``CASE``, window
    or scalar subquery, and a derived table's column of one, is its own base
    column. So is ``literal_column()``, which may spell anything.
    """
    return all(isinstance(base, sa.ColumnClause) and not base.is_literal for base in column.base_columns)


def prepare(
    stmt: sa.sql.ClauseElement,
    params: Params | Sequence[Params] | None,
    connection: BaseDatabaseWrapper,
) -> Prepared:
    """Compile ``stmt`` for ``connection`` and convert ``params`` through the Django fields.

    ``params`` is ``None``, one mapping, or a sequence of mappings (executemany).
    The compilation is reused when the same statement object runs again on the
    same database vendor with params of the same names, and the result
    converters of a column type, for a table column and for an expression, are
    found once per connection object; the
    parameters and the RETURNING check are worked out on every call.
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
        _refuse_empty_wrapped_lists(entry.wrapped_lists, state, connection)
        if sql is None:
            sql = state.statement
        elif state.statement != sql:
            raise ValueError("every executemany row must compile to the same SQL (check expanding IN lists)")
        param_rows.append(_positional(compiled, state, entry.lookups, connection))
    converters = _result_converters(entry.types, entry.direct, connection)
    return Prepared(
        sql or compiled.string,
        param_rows,
        many,
        converters,
        rowcount_only=entry.dml and not entry.types,
        names=entry.names,
    )


def _refuse_empty_wrapped_lists(names: frozenset[str], state: Any, connection: BaseDatabaseWrapper) -> None:
    # SQLAlchemy would write its empty set inside each value's bind_expression():
    # meta IN (JSON_EXTRACT(NULL) AND (1 != 1, '$')), which MySQL refuses (#90).
    empty = sorted(name for name in names if state.parameter_expansion.get(name) == [])
    if empty:
        raise NotSupportedError(
            f"{connection.display_name} cannot compare a JSON value IN the empty list given for the expanding "
            f"bindparam {empty[0]!r}: SQLAlchemy writes the empty set inside JSON_EXTRACT(). Pass a literal "
            "list (meta.in_([]) compiles to 1 != 1) or skip the condition when the list is empty."
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
    # A tuple's ("keys_1_1", "keys_1_2", "keys_2_1", ...) come row by row, each member
    # taking the type of its place in the tuple.
    origin = {
        name: (original, i) for original, names in state.parameter_expansion.items() for i, name in enumerate(names)
    }
    values = []
    for name in state.positiontup or ():
        key, i = origin.get(name, (name, 0))
        bind = compiled.binds.get(key)
        type_ = bind.type if bind is not None else None
        if isinstance(type_, sa.types.TupleType):
            type_ = type_.types[i % len(type_.types)]
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


# Result converters by column type, then by whether a table column (True) or
# an expression (False) has it, kept on the connection object they were found
# for (connections[alias], one per thread). They are bound methods or closures
# of that object's ops, so in a module-level map keyed weakly by the
# connection they would keep it alive. Through the django.db.connection proxy,
# getattr and setattr reach the calling thread's own object. The column types
# are held weakly: a table rebuilt after forget_table() has new ones.
_CONVERTERS = "_django_sqlalchemy_bridge_converters"


def _result_converters(
    types: Sequence[sa.types.TypeEngine], direct: Sequence[bool], connection: BaseDatabaseWrapper
) -> list[tuple[tuple[Any, ...], Any] | None]:
    cache = getattr(connection, _CONVERTERS, None)
    if cache is None:
        cache = weakref.WeakKeyDictionary()
        setattr(connection, _CONVERTERS, cache)
    out = []
    for type_, column in zip(types, direct, strict=True):
        if not isinstance(type_, FieldType):
            out.append(None)
            continue
        found = cache.setdefault(type_, {})
        if column not in found:
            found[column] = _find_converters(type_.field, column, connection)
        out.append(found[column])
    return out


def _find_converters(field: Any, column: bool, connection: BaseDatabaseWrapper) -> tuple[tuple[Any, ...], Any] | None:
    # The ORM reads a column through Col(field) and anything else (Func,
    # CombinedExpression, Case, Window, Subquery) through the expression, which
    # has only its output_field: the backend's converters first, then the
    # expression's (SQLCompiler.get_converters()). The backends tell them apart:
    # SQLite quantizes a Col's decimal to its field's decimal_places and
    # max_digits, an expression's only to 15 significant digits. An
    # expression's own converters coerce a number to its field's type
    # (BaseExpression.convert_value), which Django's Oracle cursor needs: it
    # gives a number with no declared precision (sum(), CASE) as int or Decimal
    # by its text.
    expression = field.get_col(field.model._meta.db_table) if column else Expression(output_field=field)
    functions = connection.ops.get_db_converters(expression) + expression.get_db_converters(connection)
    return (tuple(functions), expression) if functions else None


def from_db(
    rows: Sequence[Sequence[Any]],
    converters: list[tuple[tuple[Any, ...], Any] | None],
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


def raw(
    model: type[Model],
    stmt: sa.sql.ClauseElement,
    params: Params | None = None,
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> RawQuerySet:
    """Return ``model``'s instances for the rows of ``stmt``, as ``Model.objects.raw()`` gives them.

    ``stmt`` is compiled for database ``using`` and ``params`` converted as in
    :func:`execute` (a value compared with a column as the ORM's lookup does)
    now, so a mistake shows before anything runs. The SQL runs on Django's
    cursor when the returned ``RawQuerySet`` is iterated, and Django builds the
    instances: a result column named as a field's column fills that field,
    converted as the ORM reads it; a field left out is deferred; any other
    column (a label) becomes an attribute holding the value as the database
    driver returned it.

    Only statements that return rows are accepted: DML (``RETURNING`` or not)
    and a sequence of params raise ``ValueError``, and so does a statement
    whose columns are known (a ``select()``) but lack the primary key. A
    ``text()`` statement is left to Django, which raises ``FieldDoesNotExist``
    when it runs without the primary key.
    """
    if params is not None and not isinstance(params, Mapping):
        raise ValueError("raw() runs one statement; pass a single mapping of params")
    if isinstance(stmt, sa.sql.dml.UpdateBase):
        raise ValueError("raw() takes a statement that selects rows (SELECT); use execute() for DML")
    connection = connections[using]
    prepared = prepare(stmt, params, connection)
    if prepared.names:
        # The names Django will read from the cursor (on Oracle, respelled as Django spells them).
        reported = {_db_name(connection, name) for name in prepared.names}
        converter = connection.introspection.identifier_converter
        missing = [f.column for f in model._meta.pk_fields if converter(f.column) not in reported]
        if missing:
            raise ValueError(
                f"raw() needs the primary key of {model.__name__}: the statement does not select "
                f"{', '.join(map(repr, missing))}"
            )
    return model._default_manager.db_manager(using).raw(prepared.sql, prepared.param_rows[0])


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


# copy_to() gathers psycopg's COPY data, which comes a row at a time, into
# chunks of at least this many bytes: a StreamingHttpResponse writes each chunk.
COPY_CHUNK_BYTES = 64 * 1024


def copy_to(
    stmt: sa.sql.ClauseElement,
    params: Params | None = None,
    *,
    format: str = "csv",
    header: bool = True,
    using: str = DEFAULT_DB_ALIAS,
) -> Iterator[bytes]:
    """Yield the rows of ``stmt`` as the CSV (or text) bytes PostgreSQL's ``COPY ... TO STDOUT`` writes.

    The statement is compiled and ``params`` converted as in :func:`execute`,
    then runs as ``COPY (<statement>) TO STDOUT`` on the psycopg 3 cursor under
    Django's, so no row becomes a Python object: ``f.writelines(copy_to(...))``
    or ``StreamingHttpResponse(copy_to(...), content_type="text/csv")``. The
    bytes come in chunks of at least 64 KiB (the last may be shorter).

    ``format`` is ``"csv"`` (with a header line of the column names unless
    ``header=False``) or ``"text"`` (PostgreSQL's tab-separated format, no
    header). The values are the server's text, not Django's: a ``timestamptz``
    in the session time zone Django set (UTC under ``USE_TZ``), booleans ``t``
    and ``f``, ``bytea`` as ``\\x...``, JSON as its text; NULL is an empty field
    in CSV (an empty string is ``""``) and ``\\N`` in text.

    Only statements that return rows: DML (``RETURNING`` or not) raises
    ``ValueError``, and PostgreSQL with psycopg 3 only, else
    ``NotSupportedError``, both before anything runs. As with :func:`stream`
    nothing runs until the first chunk is asked for, and the cursor closes when
    the bytes run out, the generator is closed, or an error ends it. Closed
    early, the ``COPY`` is cancelled, unless a transaction is open: cancelling
    would abort it, so the rest is read and dropped. The ``COPY`` shows up in
    ``connection.queries`` but not in ``execute_wrapper()``.
    """
    if format not in ("csv", "text"):
        raise ValueError(f"format must be 'csv' or 'text', got {format!r}")
    if header and format != "csv":
        raise ValueError("header=True needs format='csv'; pass header=False for the text format")
    if isinstance(stmt, sa.sql.dml.UpdateBase):
        raise ValueError("copy_to() takes a statement that selects rows (SELECT); use execute() for DML")
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise NotSupportedError(f"copy_to() needs PostgreSQL, not {connection.display_name}")
    # psycopg2 has copy_expert() rather than psycopg 3's Cursor.copy().
    if getattr(connection.Database, "Copy", None) is None:
        raise NotSupportedError("copy_to() needs psycopg 3, not psycopg2")
    prepared = _prepare_one(stmt, params, connection, "copy_to")
    options = f"FORMAT {format}" + (f", HEADER {'true' if header else 'false'}" if format == "csv" else "")
    return _copy_to(connection, prepared, options)


def _copy_to(connection: BaseDatabaseWrapper, prepared: Prepared, options: str) -> Iterator[bytes]:
    cursor = connection.cursor()
    try:
        # PostgreSQL takes no parameters in COPY; psycopg writes them into the
        # SQL itself, but only when there are some, so "%%" would stay doubled
        # without. A ClientCursor's mogrify() writes them as psycopg would, with
        # the connection's adapters, and turns "%%" into "%" even with none.
        query = connection.Database.ClientCursor(connection.connection).mogrify(prepared.sql, prepared.param_rows[0])
        sql = f"COPY ({query}) TO STDOUT WITH ({options})"
        # The psycopg cursor's copy() goes around Django's execute(): check the
        # transaction, log the statement and wrap errors as execute() does (see bulk._copy).
        connection.validate_no_broken_transaction()
        logged = cursor.debug_sql(sql) if connection.queries_logged else contextlib.nullcontext()
        with logged, connection.wrap_database_errors, cursor.cursor.copy(sql) as copy:
            chunk: list[Any] = []
            size = 0
            try:
                for data in copy:
                    chunk.append(data)
                    size += len(data)
                    if size >= COPY_CHUNK_BYTES:
                        yield b"".join(chunk)
                        chunk, size = [], 0
            except GeneratorExit:
                # Leaving the block early, psycopg cancels the COPY, which in a
                # transaction aborts it. There, read the rest instead.
                if not connection.get_autocommit():
                    for _ in copy:
                        pass
                raise
            if chunk:
                yield b"".join(chunk)
    finally:
        cursor.close()
