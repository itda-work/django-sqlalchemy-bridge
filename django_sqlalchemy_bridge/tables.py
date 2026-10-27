"""Build SQLAlchemy ``Table`` objects from Django models, and check them for drift.

The Django model is the single source of truth for the schema. The tables built
here exist so SQLAlchemy can compile statements; the SQLAlchemy column types are
generic and are not used to convert values. Each column's type is a
:class:`FieldType` wrapping the generic type and the Django field the column came
from (also in ``Column.info["django_field"]``), so value conversion can follow
Django instead: a bound value compared with or assigned to the column carries
that type, and so does a result column that selects it.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from django.conf import settings
from django.db import connections, models
from django.db.backends.utils import truncate_name
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.dialects.postgresql import operators as pg_operators
from sqlalchemy.sql import operators

__all__ = [
    "Difference",
    "FieldType",
    "UnsupportedFieldError",
    "assert_table_matches",
    "compare_table",
    "forget_table",
    "metadata",
    "table_for",
]

#: The one ``MetaData`` every table built by :func:`table_for` belongs to.
metadata = sa.MetaData()

_tables: dict[type[models.Model], sa.Table] = {}
_lock = threading.RLock()


class UnsupportedFieldError(TypeError):
    """A model field has a database column but no SQLAlchemy mapping here."""


# Operators whose other side is a value of the column itself, so it converts like
# the column. Others (LIKE, arithmetic, JSON paths) take the generic type's rule.
_SAME_VALUE_OPERATORS = frozenset(
    {
        operators.eq,
        operators.ne,
        operators.lt,
        operators.le,
        operators.gt,
        operators.ge,
        operators.in_op,
        operators.not_in_op,
        operators.between_op,
        operators.not_between_op,
        operators.is_distinct_from,
        operators.is_not_distinct_from,
        # jsonb @> and <@ take a JSON document, as Django's contains/contained_by do.
        pg_operators.CONTAINS,
        pg_operators.CONTAINED_BY,
    }
)


class FieldType(sa.types.TypeDecorator):
    """A column type that compiles as ``impl`` and remembers the Django ``field``.

    The bridge never runs SQLAlchemy's bind or result processing; it reads
    ``field`` off bound parameters and selected columns and converts values the
    way Django does. ``impl`` is the generic SQLAlchemy type listed in the README.
    """

    impl = sa.types.NullType
    cache_ok = True

    def __init__(self, impl: sa.types.TypeEngine, field: models.Field) -> None:
        super().__init__()
        self.impl = impl
        self.field = field

    def bind_processor(self, dialect: sa.engine.Dialect) -> None:
        # Values convert through the field, never through the generic type's
        # processor, which SQLAlchemy 2.0 still builds for every bind (and which,
        # for JSON on its Oracle dialect, cannot be built).
        return None

    def coerce_compared_value(self, op: Any, value: Any) -> sa.types.TypeEngine:
        if op in _SAME_VALUE_OPERATORS:
            return self
        return self.impl.coerce_compared_value(op, value)

    def __repr__(self) -> str:
        return f"FieldType({self.impl!r}, {self.field.model._meta.label}.{self.field.name})"


def _generic_type(type_: sa.types.TypeEngine) -> sa.types.TypeEngine:
    """The generic type under a :class:`FieldType`, or ``type_`` itself."""
    return type_.impl if isinstance(type_, FieldType) else type_


class _MySQLJSON(mysql.JSON):
    """MySQL's JSON, with each bound value read as a JSON document.

    Django binds a JSON value as its text. MySQL compares a JSON value with a
    string as a JSON string, so ``meta["k"] == 1`` would match ``{"k": "1"}``;
    the ORM wraps the value in ``JSON_EXTRACT(%s, '$')`` (MySQL and MariaDB), and
    so does this. Storing the wrapped value stores the same document.
    """

    cache_ok = True

    def bind_expression(self, bindvalue: Any) -> Any:
        return sa.func.JSON_EXTRACT(bindvalue, sa.literal_column("'$'"), type_=self)


# SQLAlchemy compiles JSON for Oracle from 2.1 on.
_SA_ORACLE_JSON = tuple(int(n) for n in sa.__version__.split(".")[:2]) >= (2, 1)


def _string(field: models.Field) -> sa.types.TypeEngine:
    return sa.String(field.max_length)


_AUTO_TYPES = {"AutoField", "BigAutoField", "SmallAutoField"}

# Keyed by Field.get_internal_type(), so subclasses that report a built-in type
# (EmailField -> CharField, ImageField -> FileField, ...) map through it.
_TYPES = {
    "AutoField": lambda f: sa.Integer(),
    "BigAutoField": lambda f: sa.BigInteger(),
    "SmallAutoField": lambda f: sa.SmallInteger(),
    "IntegerField": lambda f: sa.Integer(),
    "PositiveIntegerField": lambda f: sa.Integer(),
    "BigIntegerField": lambda f: sa.BigInteger(),
    "PositiveBigIntegerField": lambda f: sa.BigInteger(),
    "SmallIntegerField": lambda f: sa.SmallInteger(),
    "PositiveSmallIntegerField": lambda f: sa.SmallInteger(),
    "CharField": _string,
    "SlugField": _string,
    "FileField": _string,
    "FilePathField": _string,
    "GenericIPAddressField": lambda f: sa.String(39),
    "TextField": lambda f: sa.Text(),
    "BooleanField": lambda f: sa.Boolean(),
    "DecimalField": lambda f: sa.Numeric(f.max_digits, f.decimal_places),
    "FloatField": lambda f: sa.Float(),
    "DateField": lambda f: sa.Date(),
    "TimeField": lambda f: sa.Time(),
    "DateTimeField": lambda f: sa.DateTime(timezone=settings.USE_TZ),
    "DurationField": lambda f: sa.Interval(),
    "UUIDField": lambda f: sa.Uuid(),
    # Django stores JSONField as jsonb on PostgreSQL. The comparator comes from the
    # type an expression is built with, before any dialect is chosen, so JSONB has
    # to be the base type for contains() and has_key() to exist; SQLite and Oracle
    # compile JSON, MySQL and MariaDB JSON with bound values read as documents.
    # SQLAlchemy 2.0's Oracle dialect has no JSON (nor JSON keys and paths).
    "JSONField": lambda f: (
        postgresql.JSONB()
        .with_variant(sa.JSON(), "sqlite", *(["oracle"] if _SA_ORACLE_JSON else []))
        .with_variant(_MySQLJSON(), "mysql", "mariadb")
    ),
    "BinaryField": lambda f: sa.LargeBinary(),
}


def _is_foreign_key(field: models.Field) -> bool:
    return field.is_relation and (field.many_to_one or field.one_to_one)


def _column_type(field: models.Field, model: type[models.Model]) -> sa.types.TypeEngine:
    if _is_foreign_key(field):
        return _column_type(field.target_field, model)
    internal = field.get_internal_type()
    try:
        make = _TYPES[internal]
    except KeyError:
        raise UnsupportedFieldError(
            f"{model._meta.label}.{field.name}: {type(field).__name__} (internal type {internal!r}) "
            "has a database column but no SQLAlchemy mapping; see the supported fields in the README"
        ) from None
    return make(field)


def _model_fields(model: type[models.Model]) -> list[models.Field]:
    # Fields with a column in this model's own table; parents' fields live in theirs.
    # Django before 6.0 marks ManyToManyField concrete although its rows live in the through table.
    return [f for f in model._meta.get_fields(include_parents=False) if f.concrete and not f.many_to_many]


def _primary_key_names(model: type[models.Model]) -> set[str]:
    pk = model._meta.pk
    if pk.concrete:
        return {pk.name}
    # CompositePrimaryKey (Django 5.2+) has no column of its own; it may name parts by attname.
    return {field.name for field in pk.fields}


def _build(model: type[models.Model], target: sa.MetaData) -> sa.Table:
    meta = model._meta
    pk_names = _primary_key_names(model)
    columns = []
    for field in _model_fields(model):
        args: list[Any] = [FieldType(_column_type(field, model), field)]
        if _is_foreign_key(field):
            target_field = field.target_field
            args.append(sa.ForeignKey(f"{target_field.model._meta.db_table}.{target_field.column}"))
        columns.append(
            sa.Column(
                field.column,
                *args,
                primary_key=field.name in pk_names,
                nullable=field.null,
                autoincrement=field.get_internal_type() in _AUTO_TYPES,
                info={"django_field": field},
            )
        )
    return sa.Table(meta.db_table, target, *columns, info={"django_model": model})


def _check_model(model: Any) -> type[models.Model]:
    if not (isinstance(model, type) and issubclass(model, models.Model)):
        raise TypeError(f"expected a Django model class, got {model!r}")
    if model._meta.abstract:
        raise TypeError(f"{model.__name__} is abstract and has no table")
    return model._meta.concrete_model


def table_for(model: type[models.Model]) -> sa.Table:
    """Return the ``sa.Table`` for ``model``, building and caching it on first use.

    The table lives in the shared :data:`metadata`; calling this again with the
    same model (or a proxy of it) returns the same object. Tables of models the
    foreign keys point at are built too, so the foreign keys resolve. Column
    types follow ``settings.USE_TZ`` as it was when the table was first built.

    The table comes from the model and belongs to no database; the ``using`` of
    ``execute()`` and the other calls picks the database it runs on.
    """
    model = _check_model(model)
    with _lock:
        table = _tables.get(model)
        if table is not None:
            return table
        pending = _uncached_closure(model)
        # Check every table this call would add before registering any, so a
        # failure leaves the cache and the metadata as they were.
        claimed = {name: table.info.get("django_model") for name, table in metadata.tables.items()}
        for each in pending:
            name = each._meta.db_table
            if name in claimed:
                raise ValueError(f"table {name!r} of {each._meta.label} is already built for {claimed[name]!r}")
            claimed[name] = each
            _build(each, sa.MetaData())
        for each in pending:
            _tables[each] = _build(each, metadata)
        return _tables[model]


def _uncached_closure(model: type[models.Model]) -> list[type[models.Model]]:
    """``model`` and the models its foreign keys reach, minus those already cached."""
    found = [model]
    for each in found:
        for field in _model_fields(each):
            if _is_foreign_key(field):
                target = field.target_field.model._meta.concrete_model
                if target not in _tables and target not in found:
                    found.append(target)
    return found


def forget_table(model: type[models.Model]) -> None:
    """Drop the cached table of ``model`` from the cache and the shared metadata.

    Tables of other models that point at it keep their foreign keys by name.
    """
    model = _check_model(model)
    with _lock:
        table = _tables.pop(model, None)
        if table is not None:
            metadata.remove(table)


@dataclass(frozen=True)
class Difference:
    """One way a ``sa.Table`` disagrees with its model or the live database.

    ``source`` is ``"model"`` or ``"db"``: what the table was compared with.
    ``kind`` is one of ``table_name``, ``table_missing``, ``missing`` (the
    source has a column the table lacks), ``unexpected`` (the table has a column
    the source lacks), ``type``, ``nullable``, ``primary_key``, ``foreign_key``.
    ``expected`` is the source's side, ``actual`` the table's.
    """

    source: str
    kind: str
    column: str | None
    expected: Any
    actual: Any

    def __str__(self) -> str:
        where = f" column {self.column!r}" if self.column else ""
        return f"[{self.source}] {self.kind}{where}: expected {self.expected!r}, table has {self.actual!r}"


def _type_repr(column: sa.Column) -> str:
    # The field the type carries is not part of the schema; compare the generic type.
    return repr(_generic_type(column.type))


def _foreign_keys(column: sa.Column) -> list[str]:
    return sorted(fk.target_fullname for fk in column.foreign_keys)


def _compare_with_model(model: type[models.Model], table: sa.Table) -> list[Difference]:
    fresh = _build(model, sa.MetaData())
    diffs = []
    if fresh.name != table.name:
        diffs.append(Difference("model", "table_name", None, fresh.name, table.name))
    for want in fresh.c:
        have = table.c.get(want.name)
        if have is None:
            diffs.append(Difference("model", "missing", want.name, _type_repr(want), None))
            continue
        for kind, get in (
            ("type", _type_repr),
            ("nullable", lambda c: c.nullable),
            ("primary_key", lambda c: c.primary_key),
            ("foreign_key", _foreign_keys),
        ):
            if get(want) != get(have):
                diffs.append(Difference("model", kind, want.name, get(want), get(have)))
    for have in table.c:
        if have.name not in fresh.c:
            diffs.append(Difference("model", "unexpected", have.name, None, _type_repr(have)))
    return diffs


# PostgreSQL's other names for a type, mapped to the name format_type() reports.
_PG_TYPE_NAMES = {
    "int": "integer",
    "int4": "integer",
    "serial": "integer",
    "serial4": "integer",
    "int8": "bigint",
    "bigserial": "bigint",
    "serial8": "bigint",
    "int2": "smallint",
    "smallserial": "smallint",
    "serial2": "smallint",
    "float8": "double precision",
    "float4": "real",
    "bool": "boolean",
    "decimal": "numeric",
    "varchar": "character varying",
    "char": "character",
    "bpchar": "character",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timetz": "time with time zone",
    "time": "time without time zone",
}

_PG_COLUMN_TYPES = """
    SELECT a.attname, format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
    WHERE c.relname = %s AND pg_table_is_visible(c.oid) AND a.attnum > 0 AND NOT a.attisdropped
"""


# MySQL's other spellings of a type, rewritten to what information_schema's
# COLUMN_TYPE reports (MySQL 8.0.19+ drops integer display widths but tinyint(1)).
_MYSQL_TYPE_SPELLINGS = [
    (r"\)(?=[a-z])", ") "),  # "int(10) unsigned" keeps its space
    (r"\s*\bauto_increment\b", ""),
    (r"^(?!tinyint\(1\))(tinyint|smallint|mediumint|int|integer|bigint)\(\d+\)", r"\1"),
    (r"^integer\b", "int"),
    (r"^bool(ean)?$", "tinyint(1)"),
    (r"^numeric\b", "decimal"),
    (r"^(double precision|real)\b", "double"),
]

_MYSQL_COLUMN_TYPES = """
    SELECT column_name, column_type FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = %s
"""


def _normalize_type(vendor: str, sql_type: str) -> str:
    """``sql_type`` spelled one way per type: lower case, single spaces, no spaces in ``(...)``."""
    text = " ".join(sql_type.lower().split())
    text = re.sub(r"\s*([(),])\s*", r"\1", text)
    if vendor == "postgresql":
        match = re.fullmatch(r"([a-z0-9_ ]+?)(\(.*\))?", text)
        if match:
            name, args = match.groups()
            text = _PG_TYPE_NAMES.get(name, name) + (args or "")
    elif vendor == "mysql":
        for pattern, replacement in _MYSQL_TYPE_SPELLINGS:
            text = re.sub(pattern, replacement, text)
    return text


def _db_column_types(connection: Any, cursor: Any, table_name: str, description: list) -> dict[str, str]:
    """The live type of each column, normalized; empty on backends whose types are not compared."""
    if connection.vendor == "sqlite":
        # The declared type, which for a table Django made is the field's db_type().
        return {info.name: _normalize_type("sqlite", info.type_code) for info in description}
    if connection.vendor == "postgresql":
        # format_type() keeps the length and precision that the type_code alone does not.
        cursor.execute(_PG_COLUMN_TYPES, [table_name])
        return {name: _normalize_type("postgresql", sql_type) for name, sql_type in cursor.fetchall()}
    if connection.vendor == "mysql" and not connection.mysql_is_mariadb:
        # COLUMN_TYPE keeps the length, precision and UNSIGNED. MariaDB is left out:
        # its JSON is longtext and its UUID a native type, so its columns are not compared.
        cursor.execute(_MYSQL_COLUMN_TYPES, [table_name])
        return {name: _normalize_type("mysql", sql_type) for name, sql_type in cursor.fetchall()}
    # Nor are Oracle's: its data dictionary spells types apart from db_type()
    # (FLOAT(126) for DOUBLE PRECISION, TIMESTAMP(6), identity columns).
    return {}


# Django's Oracle backend keeps identifiers to 30 characters (DatabaseOperations.max_name_length()).
_ORACLE_MAX_NAME_LENGTH = 30


def _oracle_name(name: str) -> str:
    """The name Django's Oracle ``quote_name()`` gives ``name``: cut past 30 characters (with a hash), upper case."""
    # str(): a quoted_name(..., quote=True) keeps its case through upper().
    return truncate_name(str(name), _ORACLE_MAX_NAME_LENGTH).upper()


def _db_name(connection: Any, name: str) -> str:
    """``name`` as the database's introspection reports it (on Oracle, respelled and in lower case)."""
    if connection.vendor == "oracle":
        name = _oracle_name(name)
    return connection.introspection.identifier_converter(name)


def _db_nullable(connection: Any, column: sa.Column) -> bool:
    """Whether the live column allows NULL for ``column``, as Django's schema editor makes it.

    Where the database stores an empty string as NULL (Oracle), a field that
    takes empty strings is nullable whatever its ``null``.
    """
    field = column.info.get("django_field")
    if (
        field is not None
        and connection.features.interprets_empty_strings_as_nulls
        and field.empty_strings_allowed
        and not field.primary_key
    ):
        return True
    return column.nullable


def _compare_with_db(model: type[models.Model], table: sa.Table, using: str) -> list[Difference]:
    connection = connections[using]
    introspection = connection.introspection
    with connection.cursor() as cursor:
        if _db_name(connection, table.name) not in introspection.table_names(cursor):
            return [Difference("db", "table_missing", None, table.name, None)]
        description = introspection.get_table_description(cursor, table.name)
        db_types = _db_column_types(connection, cursor, table.name, description)
    fields = {_db_name(connection, field.column): field for field in _model_fields(model)}
    columns = {_db_name(connection, column.name): column for column in table.c}
    diffs = []
    in_db = set()
    for info in description:
        in_db.add(info.name)
        have = columns.get(info.name)
        if have is None:
            try:
                db_type = introspection.get_field_type(info.type_code, info)
            except KeyError:
                db_type = str(info.type_code)
            diffs.append(Difference("db", "missing", info.name, db_type, None))
            continue
        field = fields.get(info.name)
        want = field.db_type(connection) if field is not None else None
        if info.name in db_types and want is not None:
            want = _normalize_type(connection.vendor, want)
            if db_types[info.name] != want:
                diffs.append(Difference("db", "type", have.name, db_types[info.name], want))
        if bool(info.null_ok) != (nullable := _db_nullable(connection, have)):
            diffs.append(Difference("db", "nullable", have.name, bool(info.null_ok), nullable))
    for name, have in columns.items():
        if name not in in_db:
            diffs.append(Difference("db", "unexpected", have.name, None, _type_repr(have)))
    return diffs


def compare_table(
    model: type[models.Model],
    table: sa.Table | None = None,
    *,
    check_db: bool = False,
    using: str = "default",
) -> list[Difference]:
    """List how ``table`` (by default the cached :func:`table_for` table) differs from ``model``.

    The table is compared with a table freshly built from the current model:
    table name, columns, types, nullability, primary key and foreign key targets.
    With ``check_db=True`` it is also compared with the live table in database
    ``using``: column names (as the backend spells them: Oracle's upper case
    names, cut past 30 characters), nullability (as Django's schema editor
    makes it: on Oracle a field that takes empty strings is nullable) and, on
    SQLite, PostgreSQL and MySQL (not MariaDB or Oracle), the column type the
    model field's ``db_type()`` asks for. An empty list means no drift.
    """
    model = _check_model(model)
    if table is None:
        table = table_for(model)
    diffs = _compare_with_model(model, table)
    if check_db:
        diffs.extend(_compare_with_db(model, table, using))
    return diffs


def assert_table_matches(
    model: type[models.Model],
    table: sa.Table | None = None,
    *,
    check_db: bool = False,
    using: str = "default",
) -> None:
    """Raise ``AssertionError`` listing every :func:`compare_table` difference, if any."""
    diffs = compare_table(model, table, check_db=check_db, using=using)
    if diffs:
        name = table.name if table is not None else model._meta.db_table
        lines = "\n".join(f"  {d}" for d in diffs)
        raise AssertionError(f"table {name!r} drifted from {model._meta.label}:\n{lines}")
