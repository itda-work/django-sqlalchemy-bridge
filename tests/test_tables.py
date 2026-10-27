import types

import pytest
import sqlalchemy as sa
from django.apps import apps
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps
from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite

from django_sqlalchemy_bridge import (
    Difference,
    FieldType,
    UnsupportedFieldError,
    assert_table_matches,
    compare_table,
    forget_table,
    metadata,
    table_for,
)
from django_sqlalchemy_bridge.execution import dialect_for
from tests.testapp.models import Author, Book, Gadget, Specimen

DIALECTS = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.dialect(),
    "mysql": mysql.dialect(),
    "oracle": oracle.dialect(),
}


@pytest.fixture
def undo_ddl(db):
    """Put back, on MySQL and Oracle, the tables a test changed with DDL.

    MySQL and Oracle commit each DDL statement at once, so the test's rollback
    cannot undo it; the tables of the models passed to ``rebuild([...])`` are
    dropped and created again from the model, the tables named in
    ``drop([...])`` dropped. Oracle drops the foreign keys into a table with it,
    so there the tables pointing at a rebuilt one are rebuilt too.
    """
    rebuild, drop = [], []
    yield types.SimpleNamespace(rebuild=rebuild.extend, drop=drop.extend)
    if connection.vendor == "oracle":
        rebuild_on_oracle(rebuild, drop)
    if connection.vendor != "mysql":
        return
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for model in rebuild:
                through = [f.remote_field.through for f in model._meta.local_many_to_many]
                for each in [*through, model]:
                    cursor.execute(f"DROP TABLE IF EXISTS {qn(each._meta.db_table)}")
                # Collected, not run: the schema editor refuses DDL inside the test's atomic().
                with connection.schema_editor(collect_sql=True) as editor:
                    editor.create_model(model)
                for sql in editor.collected_sql:
                    cursor.execute(sql.rstrip(";"))
            for name in drop:
                cursor.execute(f"DROP TABLE IF EXISTS {qn(name)}")
        finally:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")


def rebuild_on_oracle(rebuild, drop):
    qn = connection.ops.quote_name
    everything = apps.get_models(include_auto_created=True)
    models_ = list(rebuild)
    for each in models_:  # grows: the tables whose foreign keys point at one being rebuilt
        for other in everything:
            if other not in models_ and any(
                f.many_to_one and f.remote_field.model is each for f in other._meta.local_fields
            ):
                models_.append(other)
    with connection.cursor() as cursor:
        for model in reversed(models_):
            cursor.execute(f"DROP TABLE IF EXISTS {qn(model._meta.db_table)} CASCADE CONSTRAINTS PURGE")
        for model in models_:
            if model._meta.auto_created:
                continue  # a many-to-many table, which create_model() makes with its model
            # Collected, not run: the schema editor refuses DDL inside the test's atomic().
            with connection.schema_editor(collect_sql=True) as editor:
                editor.create_model(model)
            for sql in editor.collected_sql:
                cursor.execute(sql.rstrip(";"))
        for name in drop:
            cursor.execute(f"DROP TABLE IF EXISTS {qn(name)} CASCADE CONSTRAINTS PURGE")


def type_repr(table, name):
    type_ = table.c[name].type
    assert isinstance(type_, FieldType)
    return repr(type_.impl)


# --- model -> Table ---------------------------------------------------------


def test_table_is_cached_per_model_in_the_shared_metadata():
    table = table_for(Book)
    assert table_for(Book) is table
    assert table.metadata is metadata
    assert metadata.tables["testapp_book"] is table


def test_table_and_column_names_follow_db_table_and_db_column():
    table = table_for(Specimen)
    assert table.name == "dsb_specimen"
    assert "label_text" in table.c
    assert "label" not in table.c
    assert "writer" in table.c


def test_columns_carry_their_django_field():
    table = table_for(Specimen)
    assert table.c.label_text.info["django_field"] is Specimen._meta.get_field("label")
    assert table.c.writer.info["django_field"] is Specimen._meta.get_field("author")
    assert table.info["django_model"] is Specimen


def test_column_types_carry_their_django_field():
    table = table_for(Specimen)
    for column in table.c:
        assert isinstance(column.type, FieldType)
        assert column.type.field is column.info["django_field"]
    # Values compared with the column take its type; LIKE patterns and arithmetic do not.
    assert (table.c.amount == 1).right.type is table.c.amount.type
    assert table.c.uid.in_([1]).right.type is table.c.uid.type
    assert not isinstance(table.c.label_text.like("a%").right.type, FieldType)
    assert repr(table.c.writer.type) == "FieldType(BigInteger(), testapp.Specimen.author)"


@pytest.mark.parametrize(
    "model, column, expected",
    [
        (Author, "id", "BigInteger()"),
        (Gadget, "id", "Integer()"),
        (Gadget, "code", "String(length=12)"),
        (Book, "title", "String(length=200)"),
        (Book, "status", "String(length=20)"),
        (Book, "price", "Numeric(precision=10, scale=2)"),
        (Book, "uid", "Uuid()"),
        (Book, "meta", "JSONB(astext_type=Text())"),
        (Specimen, "uid", "Uuid()"),
        (Specimen, "body", "Text()"),
        (Specimen, "slug", "String(length=50)"),
        (Specimen, "email", "String(length=254)"),
        (Specimen, "small", "SmallInteger()"),
        (Specimen, "positive", "Integer()"),
        (Specimen, "big", "BigInteger()"),
        (Specimen, "flag", "Boolean()"),
        (Specimen, "ratio", "Float()"),
        (Specimen, "amount", "Numeric(precision=12, scale=3)"),
        (Specimen, "day", "Date()"),
        (Specimen, "at", "Time()"),
        (Specimen, "happened_at", "DateTime(timezone=True)"),
        (Specimen, "took", "Interval()"),
        (Specimen, "blob", "LargeBinary()"),
        (Specimen, "level", "Integer()"),
    ],
)
def test_field_types(model, column, expected):
    assert type_repr(table_for(model), column) == expected


def test_foreign_key_column_takes_the_target_column_type():
    table = table_for(Specimen)
    assert type_repr(table, "writer") == "BigInteger()"  # Author.id is a BigAutoField
    assert type_repr(table, "gadget_id") == "String(length=12)"  # to_field="code"
    assert type_repr(table, "twin_id") == "Uuid()"  # self reference to a UUID pk


def test_foreign_keys_point_at_the_target_column():
    table = table_for(Specimen)
    assert {fk.target_fullname for fk in table.c.writer.foreign_keys} == {"testapp_author.id"}
    assert {fk.target_fullname for fk in table.c.gadget_id.foreign_keys} == {"testapp_gadget.code"}
    assert {fk.target_fullname for fk in table.c.twin_id.foreign_keys} == {"dsb_specimen.uid"}
    # The target tables are registered too, so the foreign keys resolve.
    assert table.c.writer.references(table_for(Author).c.id)
    assert table_for(Book).c.author_id.references(table_for(Author).c.id)


def test_primary_key_and_nullability():
    table = table_for(Specimen)
    assert [c.name for c in table.primary_key] == ["uid"]
    assert table.c.email.nullable
    assert table.c.maybe.nullable
    assert table.c.gadget_id.nullable
    assert not table.c.label_text.nullable
    assert not table.c.writer.nullable
    assert table_for(Author).c.id.autoincrement is True
    assert table.c.uid.autoincrement is False


def test_many_to_many_and_reverse_relations_are_skipped():
    names = set(table_for(Specimen).c.keys())
    assert "tags" not in names
    assert not any(name.startswith("specimen") for name in names)
    assert "books" not in table_for(Author).c


def test_datetime_timezone_follows_use_tz():
    with override_settings(USE_TZ=False):
        with isolate_apps("tests.testapp"):

            class Naive(models.Model):
                at = models.DateTimeField()

                class Meta:
                    app_label = "testapp"

            try:
                assert type_repr(table_for(Naive), "at") == "DateTime()"
            finally:
                forget_table(Naive)
    assert "testapp_naive" not in metadata.tables


@isolate_apps("tests.testapp")
def test_unsupported_concrete_field_raises():
    class PointField(models.Field):
        def db_type(self, connection):
            return "point"

    class Place(models.Model):
        where = PointField()

        class Meta:
            app_label = "testapp"

    with pytest.raises(UnsupportedFieldError, match=r"Place\.where.*PointField"):
        table_for(Place)
    assert "testapp_place" not in metadata.tables


@isolate_apps("tests.testapp")
def test_unsupported_field_behind_a_foreign_key_leaves_nothing_cached():
    class WeirdField(models.Field):
        def db_type(self, connection):
            return "text"

    class Target(models.Model):
        w = WeirdField()

        class Meta:
            app_label = "testapp"

    class Source(models.Model):
        target = models.ForeignKey(Target, on_delete=models.CASCADE)

        class Meta:
            app_label = "testapp"

    for _ in range(2):
        with pytest.raises(UnsupportedFieldError, match=r"Target\.w"):
            table_for(Source)
        assert "testapp_source" not in metadata.tables
        assert "testapp_target" not in metadata.tables


@isolate_apps("tests.testapp")
def test_abstract_model_is_rejected():
    class Base(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            abstract = True
            app_label = "testapp"

    with pytest.raises(TypeError, match="abstract"):
        table_for(Base)


@pytest.mark.parametrize("dialect", DIALECTS.values(), ids=DIALECTS.keys())
def test_tables_compile_on_every_dialect(dialect):
    # SQLAlchemy 2.0's Oracle dialect has no JSON column DDL (Django makes the schema anyway).
    ddl = not (dialect.name == "oracle" and tuple(int(n) for n in sa.__version__.split(".")[:2]) < (2, 1))
    for model in (Author, Book, Gadget, Specimen):
        if ddl:
            str(sa.schema.CreateTable(table_for(model)).compile(dialect=dialect))
        str(sa.select(table_for(model)).compile(dialect=dialect))


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Author, Book, Gadget, Specimen])
def test_select_from_the_table_runs_on_the_live_database(model):
    stmt = sa.select(table_for(model))
    names = list(table_for(model).c.keys())
    if connection.vendor == "oracle":
        # SQLAlchemy's own Oracle dialect quotes lower case reserved words ("uid",
        # "level"), naming columns Django never made; the bridge's spells them as Django does.
        sql = str(stmt.compile(dialect=dialect_for(connection)))
        expected = [name.upper() for name in names]
    else:
        sql = str(stmt.compile(dialect=DIALECTS[connection.vendor]))
        expected = names
    with connection.cursor() as cursor:
        cursor.execute(sql)
        assert [col[0] for col in cursor.description] == expected


ALL_MODELS = [
    model
    for label in ("testapp", "benchapp")
    for model in apps.get_app_config(label).get_models(include_auto_created=True)
]


@pytest.mark.django_db
@pytest.mark.parametrize("model", ALL_MODELS, ids=[m._meta.label for m in ALL_MODELS])
def test_tables_match_models_and_live_database(model):
    assert compare_table(model, check_db=True) == []
    assert_table_matches(model, check_db=True)


# --- drift -----------------------------------------------------------------


def stale_author(*extra, name_type=sa.String(100)):
    return sa.Table(
        "testapp_author",
        sa.MetaData(),
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", name_type, nullable=False),
        *extra,
    )


def test_drift_detects_a_column_the_table_lacks():
    table = stale_author()  # no joined_at
    assert compare_table(Author, table) == [
        Difference("model", "missing", "joined_at", expected="DateTime(timezone=True)", actual=None)
    ]


def test_drift_detects_a_column_the_model_dropped():
    table = stale_author(
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nickname", sa.String(20)),
    )
    assert compare_table(Author, table) == [
        Difference("model", "unexpected", "nickname", expected=None, actual="String(length=20)")
    ]


def test_drift_detects_a_type_change():
    table = stale_author(
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        name_type=sa.String(50),
    )
    assert compare_table(Author, table) == [
        Difference("model", "type", "name", expected="String(length=100)", actual="String(length=50)")
    ]


def test_drift_detects_nullability_primary_key_and_foreign_key_changes():
    table = sa.Table(
        "testapp_book",
        sa.MetaData(),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("uid", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), sa.ForeignKey("other.id"), nullable=False),
    )
    diffs = {(d.kind, d.column): d for d in compare_table(Book, table)}
    assert set(diffs) == {("primary_key", "id"), ("nullable", "title"), ("foreign_key", "author_id")}
    assert diffs["foreign_key", "author_id"].expected == ["testapp_author.id"]
    assert diffs["foreign_key", "author_id"].actual == ["other.id"]


def test_drift_detects_a_json_column_without_the_jsonb_operators():
    # A plain JSON column has no contains()/has_key(); contains() would compile to LIKE.
    table = table_for(Book).to_metadata(sa.MetaData())
    table.c.meta.type = sa.JSON()
    assert compare_table(Book, table) == [
        Difference("model", "type", "meta", expected="JSONB(astext_type=Text())", actual="JSON()")
    ]


def test_json_column_compiles_as_jsonb_on_postgresql_and_json_elsewhere():
    create = sa.schema.CreateTable(table_for(Book))
    assert "meta JSONB NOT NULL" in str(create.compile(dialect=DIALECTS["postgresql"]))
    assert "meta JSON NOT NULL" in str(create.compile(dialect=DIALECTS["sqlite"]))
    assert "meta JSON NOT NULL" in str(create.compile(dialect=DIALECTS["mysql"]))


def test_drift_detects_a_renamed_table():
    table = table_for(Author).to_metadata(sa.MetaData(), name="authors")
    assert compare_table(Author, table) == [
        Difference("model", "table_name", None, expected="testapp_author", actual="authors")
    ]


def test_assert_table_matches_lists_every_difference():
    with pytest.raises(AssertionError) as excinfo:
        assert_table_matches(Author, stale_author(name_type=sa.Text()))
    message = str(excinfo.value)
    assert "testapp_author" in message
    assert "name" in message and "Text()" in message
    assert "joined_at" in message


@pytest.mark.django_db
def test_drift_against_the_database_detects_added_and_dropped_columns(undo_ddl):
    undo_ddl.rebuild([Author])
    with connection.cursor() as cursor:
        if connection.vendor == "oracle":
            cursor.execute("ALTER TABLE testapp_author ADD (extra NUMBER(11) NULL)")
        else:
            cursor.execute("ALTER TABLE testapp_author ADD COLUMN extra integer NULL")
        cursor.execute("ALTER TABLE testapp_author DROP COLUMN joined_at")
    # The model still matches the table; only the database moved.
    assert compare_table(Author, check_db=True) == [
        Difference("db", "missing", "extra", expected="IntegerField", actual=None),
        Difference("db", "unexpected", "joined_at", expected=None, actual="DateTime(timezone=True)"),
    ]


@pytest.mark.django_db
def test_drift_against_the_database_detects_nullability(undo_ddl):
    undo_ddl.rebuild([Author])
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("ALTER TABLE testapp_author ALTER COLUMN name DROP NOT NULL")
        elif connection.vendor == "mysql":
            cursor.execute("ALTER TABLE testapp_author MODIFY name varchar(100) NULL")
        elif connection.vendor == "oracle":
            # Django makes a CharField nullable on Oracle (an empty string is NULL there),
            # so NOT NULL is the drift; a non-string column drifts the other way.
            cursor.execute("ALTER TABLE testapp_author MODIFY (name NOT NULL, joined_at NULL)")
            assert compare_table(Author, check_db=True) == [
                Difference("db", "nullable", "name", expected=False, actual=True),
                Difference("db", "nullable", "joined_at", expected=True, actual=False),
            ]
            return
        else:
            cursor.execute("ALTER TABLE testapp_author DROP COLUMN name")
            cursor.execute("ALTER TABLE testapp_author ADD COLUMN name varchar(100) NULL")
    assert compare_table(Author, check_db=True) == [Difference("db", "nullable", "name", expected=True, actual=False)]


def drop_indexes_on(table, column):
    def run(cursor):
        for name, info in connection.introspection.get_constraints(cursor, table).items():
            if info["index"] and info["columns"] == [column]:
                cursor.execute(f"DROP INDEX {connection.ops.quote_name(name)}")

    return run


def drop_foreign_keys_on(table, column):
    def run(cursor):
        for name, info in connection.introspection.get_constraints(cursor, table).items():
            if info["foreign_key"] and info["columns"] == [column]:
                cursor.execute(f"ALTER TABLE {table} DROP FOREIGN KEY {connection.ops.quote_name(name)}")

    return run


# Each case changes the live columns the way a missed migration would: {vendor: (SQL, differences)}.
# On SQLite a column is dropped and added again, as SQLite cannot change a column's type in place.
DB_TYPE_DRIFT = {
    "varchar_length": {
        "mysql": (
            ["ALTER TABLE testapp_author MODIFY name varchar(50) NOT NULL"],
            [Difference("db", "type", "name", expected="varchar(50)", actual="varchar(100)")],
        ),
        "postgresql": (
            ["ALTER TABLE testapp_author ALTER COLUMN name TYPE varchar(50)"],
            [Difference("db", "type", "name", expected="character varying(50)", actual="character varying(100)")],
        ),
        "sqlite": (
            [
                "ALTER TABLE testapp_author DROP COLUMN name",
                "ALTER TABLE testapp_author ADD COLUMN name varchar(50) NOT NULL DEFAULT ''",
            ],
            [Difference("db", "type", "name", expected="varchar(50)", actual="varchar(100)")],
        ),
    },
    "type": {
        "mysql": (
            ["ALTER TABLE testapp_author MODIFY joined_at datetime NOT NULL"],
            # Without (6) the column drops the microseconds Django writes.
            [Difference("db", "type", "joined_at", expected="datetime", actual="datetime(6)")],
        ),
        "postgresql": (
            ["ALTER TABLE testapp_author ALTER COLUMN joined_at TYPE timestamp without time zone"],
            [
                Difference(
                    "db",
                    "type",
                    "joined_at",
                    expected="timestamp without time zone",
                    actual="timestamp with time zone",
                )
            ],
        ),
        "sqlite": (
            [
                "ALTER TABLE testapp_author DROP COLUMN joined_at",
                "ALTER TABLE testapp_author ADD COLUMN joined_at date NOT NULL DEFAULT '2026-01-01'",
            ],
            [Difference("db", "type", "joined_at", expected="date", actual="datetime")],
        ),
    },
    "numeric_precision": {
        "mysql": (
            ["ALTER TABLE testapp_book MODIFY price numeric(12, 3) NOT NULL"],
            [Difference("db", "type", "price", expected="decimal(12,3)", actual="decimal(10,2)")],
        ),
        "postgresql": (
            ["ALTER TABLE testapp_book ALTER COLUMN price TYPE numeric(12, 3)"],
            [Difference("db", "type", "price", expected="numeric(12,3)", actual="numeric(10,2)")],
        ),
    },
    "foreign_key_target_type": {
        "mysql": (
            [
                # MySQL refuses a foreign key between columns of different types.
                drop_foreign_keys_on("testapp_book", "author_id"),
                "ALTER TABLE testapp_book MODIFY author_id integer NOT NULL",
            ],
            [Difference("db", "type", "author_id", expected="int", actual="bigint")],
        ),
        "postgresql": (
            ["ALTER TABLE testapp_book ALTER COLUMN author_id TYPE integer"],
            [Difference("db", "type", "author_id", expected="integer", actual="bigint")],
        ),
        "sqlite": (
            [
                drop_indexes_on("dsb_specimen", "gadget_id"),
                "ALTER TABLE dsb_specimen DROP COLUMN gadget_id",
                "ALTER TABLE dsb_specimen ADD COLUMN gadget_id varchar(20) NULL",
            ],
            [Difference("db", "type", "gadget_id", expected="varchar(20)", actual="varchar(12)")],
        ),
    },
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", DB_TYPE_DRIFT)
def test_drift_against_the_database_detects_column_types(case, undo_ddl):
    if connection.vendor not in DB_TYPE_DRIFT[case] or getattr(connection, "mysql_is_mariadb", False):
        pytest.skip(f"{connection.display_name} does not report this from its columns")
    statements, expected = DB_TYPE_DRIFT[case][connection.vendor]
    sql = next(each for each in statements if isinstance(each, str) and each.startswith("ALTER"))
    model = {"testapp_author": Author, "testapp_book": Book, "dsb_specimen": Specimen}[sql.split()[2]]
    undo_ddl.rebuild([model])
    with connection.cursor() as cursor:
        for each in statements:
            each(cursor) if callable(each) else cursor.execute(each)
    assert compare_table(model, check_db=True) == expected


@isolate_apps("tests.testapp")
@pytest.mark.django_db
def test_type_check_accepts_the_spellings_a_hand_written_table_uses(undo_ddl):
    # Synonyms and spacing the database accepts are the same type, not drift.
    class Handmade(models.Model):
        id = models.AutoField(primary_key=True)
        name = models.CharField(max_length=20)
        price = models.DecimalField(max_digits=10, decimal_places=2)
        at = models.DateTimeField()
        when = models.TimeField()
        flag = models.BooleanField()
        n = models.IntegerField()
        big = models.BigIntegerField()
        ratio = models.FloatField()

        class Meta:
            app_label = "testapp"
            db_table = "dsb_handmade"

    if connection.vendor == "oracle":
        pytest.skip("Oracle column types are not compared")
    if connection.vendor == "postgresql":
        columns = (
            "id serial PRIMARY KEY, name VARCHAR( 20 ) NOT NULL, price DECIMAL(10,2) NOT NULL,"
            ' at timestamptz NOT NULL, "when" time NOT NULL, flag bool NOT NULL, n int4 NOT NULL,'
            " big int8 NOT NULL, ratio float8 NOT NULL"
        )
    elif connection.vendor == "mysql":
        columns = (
            "id INT(11) NOT NULL AUTO_INCREMENT PRIMARY KEY, name VARCHAR( 20 ) NOT NULL, price NUMERIC(10,2) NOT NULL,"
            " at DATETIME(6) NOT NULL, `when` TIME(6) NOT NULL, flag BOOLEAN NOT NULL, n INTEGER NOT NULL,"
            " big BIGINT(20) NOT NULL, ratio REAL NOT NULL"
        )
    else:
        columns = (
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, name VARCHAR( 20 ) NOT NULL, price Decimal NOT NULL,"
            ' at DATETIME NOT NULL, "when" TIME NOT NULL, flag BOOL NOT NULL, n INTEGER NOT NULL,'
            " big BIGINT NOT NULL, ratio REAL NOT NULL"
        )
    undo_ddl.drop(["dsb_handmade"])
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE dsb_handmade ({columns})")
    try:
        assert compare_table(Handmade, check_db=True) == []
    finally:
        forget_table(Handmade)


@pytest.mark.django_db
def test_drift_against_a_missing_table(undo_ddl):
    undo_ddl.rebuild([Specimen])
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE dsb_specimen_tags")
        cursor.execute("DROP TABLE dsb_specimen")
    assert compare_table(Specimen, check_db=True) == [
        Difference("db", "table_missing", None, expected="dsb_specimen", actual=None)
    ]


@isolate_apps("tests.testapp")
def test_multi_table_inheritance_keeps_parent_columns_in_the_parent_table():
    class Animal(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            app_label = "testapp"

    class Dog(Animal):
        good = models.BooleanField(default=True)

        class Meta:
            app_label = "testapp"

    try:
        table = table_for(Dog)
        assert list(table.c.keys()) == ["animal_ptr_id", "good"]
        assert [c.name for c in table.primary_key] == ["animal_ptr_id"]
        assert table.c.animal_ptr_id.references(table_for(Animal).c.id)
    finally:
        forget_table(Dog)
        forget_table(Animal)


@isolate_apps("tests.testapp")
def test_composite_primary_key():
    class Membership(models.Model):
        pk = models.CompositePrimaryKey("author_id", "group")
        author = models.ForeignKey(Author, on_delete=models.CASCADE)
        group = models.CharField(max_length=10)

        class Meta:
            app_label = "testapp"

    try:
        table = table_for(Membership)
        assert list(table.c.keys()) == ["author_id", "group"]
        assert [c.name for c in table.primary_key] == ["author_id", "group"]
        assert compare_table(Membership) == []
    finally:
        forget_table(Membership)
