"""Build SQLAlchemy ``Table`` objects from Django models, and check them for drift.

The Django model is the single source of truth for the schema. The tables built
here exist so SQLAlchemy can compile statements; the SQLAlchemy column types are
generic and are not used to convert values. Each ``Column.info["django_field"]``
holds the Django field the column came from, so value conversion can follow
Django instead.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from django.conf import settings
from django.db import connections, models

__all__ = [
    "Difference",
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
    "JSONField": lambda f: sa.JSON(),
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
        args: list[Any] = [_column_type(field, model)]
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


def table_for(model: type[models.Model], *, using: str = "default") -> sa.Table:
    """Return the ``sa.Table`` for ``model``, building and caching it on first use.

    The table lives in the shared :data:`metadata`; calling this again with the
    same model (or a proxy of it) returns the same object. Tables of models the
    foreign keys point at are built too, so the foreign keys resolve. Column
    types follow ``settings.USE_TZ`` as it was when the table was first built.

    ``using`` is reserved for multiple databases; only ``"default"`` is accepted
    for now.
    """
    if using != "default":
        raise NotImplementedError("only the 'default' database is supported for now")
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
            diffs.append(Difference("model", "missing", want.name, repr(want.type), None))
            continue
        for kind, get in (
            ("type", lambda c: repr(c.type)),
            ("nullable", lambda c: c.nullable),
            ("primary_key", lambda c: c.primary_key),
            ("foreign_key", _foreign_keys),
        ):
            if get(want) != get(have):
                diffs.append(Difference("model", kind, want.name, get(want), get(have)))
    for have in table.c:
        if have.name not in fresh.c:
            diffs.append(Difference("model", "unexpected", have.name, None, repr(have.type)))
    return diffs


def _compare_with_db(table: sa.Table, using: str) -> list[Difference]:
    connection = connections[using]
    introspection = connection.introspection
    with connection.cursor() as cursor:
        if table.name not in introspection.table_names(cursor):
            return [Difference("db", "table_missing", None, table.name, None)]
        description = introspection.get_table_description(cursor, table.name)
    diffs = []
    in_db = set()
    for info in description:
        in_db.add(info.name)
        have = table.c.get(info.name)
        if have is None:
            try:
                db_type = introspection.get_field_type(info.type_code, info)
            except KeyError:
                db_type = str(info.type_code)
            diffs.append(Difference("db", "missing", info.name, db_type, None))
        elif bool(info.null_ok) != have.nullable:
            diffs.append(Difference("db", "nullable", info.name, bool(info.null_ok), have.nullable))
    for have in table.c:
        if have.name not in in_db:
            diffs.append(Difference("db", "unexpected", have.name, None, repr(have.type)))
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
    ``using`` (column names and nullability only). An empty list means no drift.
    """
    model = _check_model(model)
    if table is None:
        table = table_for(model)
    diffs = _compare_with_model(model, table)
    if check_db:
        diffs.extend(_compare_with_db(table, using))
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
