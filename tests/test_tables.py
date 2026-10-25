import pytest
import sqlalchemy as sa
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps
from sqlalchemy.dialects import postgresql, sqlite

from django_sqlalchemy_bridge import (
    Difference,
    UnsupportedFieldError,
    assert_table_matches,
    compare_table,
    forget_table,
    metadata,
    table_for,
)
from tests.testapp.models import Author, Book, Gadget, Specimen

DIALECTS = {"sqlite": sqlite.dialect(), "postgresql": postgresql.dialect()}


def type_repr(table, name):
    return repr(table.c[name].type)


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
        (Book, "meta", "JSON()"),
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
def test_abstract_model_is_rejected():
    class Base(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            abstract = True
            app_label = "testapp"

    with pytest.raises(TypeError, match="abstract"):
        table_for(Base)


def test_only_the_default_database_for_now():
    with pytest.raises(NotImplementedError):
        table_for(Author, using="other")


@pytest.mark.parametrize("dialect", DIALECTS.values(), ids=DIALECTS.keys())
def test_tables_compile_on_both_dialects(dialect):
    for model in (Author, Book, Gadget, Specimen):
        str(sa.schema.CreateTable(table_for(model)).compile(dialect=dialect))
        str(sa.select(table_for(model)).compile(dialect=dialect))


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Author, Book, Gadget, Specimen])
def test_select_from_the_table_runs_on_the_live_database(model):
    stmt = sa.select(table_for(model))
    sql = str(stmt.compile(dialect=DIALECTS[connection.vendor]))
    with connection.cursor() as cursor:
        cursor.execute(sql)
        assert [col[0] for col in cursor.description] == list(table_for(model).c.keys())


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Author, Book, Gadget, Specimen])
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
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), sa.ForeignKey("other.id"), nullable=False),
    )
    diffs = {(d.kind, d.column): d for d in compare_table(Book, table)}
    assert set(diffs) == {("primary_key", "id"), ("nullable", "title"), ("foreign_key", "author_id")}
    assert diffs["foreign_key", "author_id"].expected == ["testapp_author.id"]
    assert diffs["foreign_key", "author_id"].actual == ["other.id"]


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
def test_drift_against_the_database_detects_added_and_dropped_columns():
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE testapp_author ADD COLUMN extra integer NULL")
        cursor.execute("ALTER TABLE testapp_author DROP COLUMN joined_at")
    # The model still matches the table; only the database moved.
    assert compare_table(Author, check_db=True) == [
        Difference("db", "missing", "extra", expected="IntegerField", actual=None),
        Difference("db", "unexpected", "joined_at", expected=None, actual="DateTime(timezone=True)"),
    ]


@pytest.mark.django_db
def test_drift_against_the_database_detects_nullability():
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("ALTER TABLE testapp_author ALTER COLUMN name DROP NOT NULL")
        else:
            cursor.execute("ALTER TABLE testapp_author DROP COLUMN name")
            cursor.execute("ALTER TABLE testapp_author ADD COLUMN name varchar(100) NULL")
    assert compare_table(Author, check_db=True) == [Difference("db", "nullable", "name", expected=True, actual=False)]


@pytest.mark.django_db
def test_drift_against_a_missing_table():
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
