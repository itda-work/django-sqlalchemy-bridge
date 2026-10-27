"""raw(model, stmt): a bridge statement as a Django RawQuerySet of model instances (#57).

The bridge compiles the statement and converts its params when raw() is
called; Django's RawQuerySet runs the SQL when it is iterated and builds the
instances, converting the model's columns as the ORM reads them and leaving
any other column (a label) as the database driver gave it.
"""

import datetime
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import FieldDoesNotExist, SynchronousOnlyOperation
from django.db import connection, connections
from django.db.models import prefetch_related_objects
from django.db.models.query import RawQuerySet
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import execute, raw, table_for
from django_sqlalchemy_bridge.execution import _ReturningCompiler
from tests.test_execute import AT as SPECIMEN_AT
from tests.test_execute import specimen_values
from tests.testapp.models import Author, Book, Gadget, OracleSpelledNameModelWithALongName, Product, Sale, Specimen

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=datetime.UTC)

authors = table_for(Author)
books = table_for(Book)
specimens = table_for(Specimen)
products = table_for(Product)
sales = table_for(Sale)


def make_author(name="a", using="default"):
    return Author.objects.using(using).create(name=name, joined_at=AT)


def make_book(author, **values):
    defaults = {"title": "t", "price": Decimal("1.00"), "meta": {}}
    return Book.objects.create(author=author, **(defaults | values))


def field_values(instance):
    """Every concrete field's value as the instance holds it (bytes for a BinaryField)."""
    values = {}
    for field in type(instance)._meta.concrete_fields:
        value = getattr(instance, field.attname)
        values[field.attname] = bytes(value) if isinstance(value, memoryview) else value
    return values


# --- what comes back -----------------------------------------------------------


def test_returns_a_lazy_raw_queryset_of_model_instances():
    author = make_author("x")
    stmt = sa.select(authors).where(authors.c.name == "x")

    with CaptureQueriesContext(connection) as queries:
        qs = raw(Author, stmt)
    assert isinstance(qs, RawQuerySet)
    assert len(queries) == 0  # nothing runs until it is iterated

    with CaptureQueriesContext(connection) as queries:
        got = list(qs)
    assert len(queries) == 1
    assert got == [author]
    assert isinstance(got[0], Author) and got[0]._state.adding is False and got[0]._state.db == "default"
    with CaptureQueriesContext(connection) as queries:
        assert list(qs) == [author]  # a RawQuerySet caches its result, as Django's does
    assert len(queries) == 0


@pytest.mark.parametrize(
    "overrides",
    [{}, {"email": "a@b.c", "maybe": False, "happened_at": None, "data": None, "level": Specimen.Level.LOW}],
    ids=["filled", "nulls"],
)
def test_every_field_reads_as_the_orm_reads_it(overrides):
    author = make_author()
    gadget = Gadget.objects.create(code="g1")
    twin = Specimen.objects.create(**specimen_values(author=author))
    created = Specimen.objects.create(**specimen_values(author=author, gadget=gadget, twin=twin) | overrides)

    stmt = sa.select(specimens).where(specimens.c.uid == sa.bindparam("uid"))
    (instance,) = raw(Specimen, stmt, {"uid": created.uid})

    assert instance.get_deferred_fields() == set()
    assert field_values(instance) == field_values(Specimen.objects.get(uid=created.uid))
    # Decimal, UUID, choices, JSON, aware datetime and bytes, as the model holds them
    assert instance.amount == Decimal("123.456") and isinstance(instance.uid, uuid.UUID)
    assert instance.level == overrides.get("level", Specimen.Level.HIGH)
    assert instance.data == overrides.get("data", {"k": [1, "두", None], "n": 1.5})
    assert bytes(instance.blob) == b"\x00\xffbytes"
    if "happened_at" not in overrides:
        assert instance.happened_at == SPECIMEN_AT and instance.happened_at.tzinfo is not None
    assert instance.gadget == gadget and instance.twin == twin and instance.author == author


def test_params_convert_as_orm_lookup_values():
    author = make_author()
    keep = Specimen.objects.create(**specimen_values(author=author))
    Specimen.objects.create(**specimen_values(author=author, amount=Decimal("1.5"), happened_at=AT))
    keep.happened_at = AT.replace(hour=1)
    keep.save()
    stmt = sa.select(specimens).where(
        specimens.c.uid == sa.bindparam("uid"),
        specimens.c.amount == sa.bindparam("amount"),
        specimens.c.happened_at == sa.bindparam("at"),
    )
    # The aware datetime in another zone, the Decimal and the UUID compare as filter() compares them.
    at = keep.happened_at.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    filters = {"uid": keep.uid, "amount": Decimal("123.456"), "happened_at": at}
    assert list(Specimen.objects.filter(**filters)) == [keep]
    assert list(raw(Specimen, stmt, {"uid": keep.uid, "amount": Decimal("123.456"), "at": at})) == [keep]


def test_json_params_compare_whole_documents_as_the_orm():
    author = make_author()
    number = make_book(author, meta={"k": 1})
    make_book(author, meta={"k": "1"})  # MySQL would match it if the param were bound as a string
    stmt = sa.select(books).where(books.c.meta == sa.bindparam("meta"))
    assert list(Book.objects.filter(meta={"k": 1})) == [number]
    assert list(raw(Book, stmt, {"meta": {"k": 1}})) == [number]


@pytest.fixture
def no_auto_pk_0(monkeypatch):
    """Make the backend refuse 0 for an AutoField when it is written, as MySQL does by default."""

    def validate_autopk_value(value):
        if value == 0:
            raise ValueError("The database backend does not accept 0 as a value for AutoField.")
        return value

    monkeypatch.setattr(connection.ops, "validate_autopk_value", validate_autopk_value)


@pytest.mark.usefixtures("no_auto_pk_0")
def test_comparing_an_auto_pk_with_zero_runs_as_the_orm_lookup():
    make_author()
    assert list(Author.objects.filter(id=0)) == []
    assert list(raw(Author, sa.select(authors).where(authors.c.id == sa.bindparam("pk")), {"pk": 0})) == []


def test_literal_percent_signs_survive_without_params():
    author = make_author()
    percent = make_book(author, title="a%b")
    make_book(author, title="ab")
    stmt = sa.select(books).where(books.c.title.like(sa.literal_column("'a%'")))
    sent = []

    def wrapper(execute_, sql, params, many, context):
        sent.append(params)
        return execute_(sql, params, many, context)

    qs = raw(Book, stmt)
    assert qs.params == ()  # an empty tuple, not None: the backends turn "%%" into "%" only then
    with connection.execute_wrapper(wrapper):
        assert sorted(b.title for b in qs) == ["a%b", "ab"]
    assert sent == [()]
    assert list(raw(Book, sa.select(books).where(books.c.title == "a%b"))) == [percent]


def test_a_text_statement_runs_as_written():
    author = make_author("x")
    stmt = sa.text("SELECT id, name, joined_at, '100%' AS pct FROM testapp_author WHERE name = :name")
    (got,) = raw(Author, stmt, {"name": "x"})
    assert got == author and got.joined_at == AT
    assert got.pct == "100%"  # a column no field takes is an attribute


def test_a_text_statement_without_the_primary_key_fails_when_iterated():
    make_author()
    qs = raw(Author, sa.text("SELECT name FROM testapp_author"))  # its columns are unknown until it runs
    with pytest.raises(FieldDoesNotExist, match="primary key"):
        list(qs)


def test_labelled_columns_become_attributes_left_as_the_driver_gives_them():
    category = Product._meta.get_field("category").related_model.objects.create(name="c")
    shirt = Product.objects.create(sku="SHIRT", category=category, price=Decimal("30.00"))
    for day in (1, 2):
        Sale.objects.create(product=shirt, sold_at=AT.replace(day=day), quantity=1, amount=Decimal("30.00"))
    stmt = (
        sa.select(
            products,
            sa.func.count(sales.c.id).label("n_sales"),
            sa.func.max(sales.c.sold_at).label("last_sold"),
        )
        .join(sales, sales.c.product_id == products.c.id)
        .group_by(*products.c)
    )

    (got,) = raw(Product, stmt)

    assert got == shirt and got.price == Decimal("30.00")
    assert got.n_sales == 2
    # Django converts only the model's columns: a label is what the driver returns,
    # on SQLite the stored text of a datetime, elsewhere a naive or aware datetime.
    if connection.vendor == "sqlite":
        assert isinstance(got.last_sold, str)
    else:
        assert isinstance(got.last_sold, datetime.datetime)


def test_fields_left_out_are_deferred_and_loaded_on_access():
    author = make_author("x")
    (got,) = raw(Author, sa.select(authors.c.id, authors.c.name))
    assert got.get_deferred_fields() == {"joined_at"}
    with CaptureQueriesContext(connection) as queries:
        assert got.joined_at == author.joined_at
    assert len(queries) == 1


def test_a_compound_select():
    a, b = make_author("a"), make_author("b")
    make_author("c")
    stmt = sa.union_all(
        sa.select(authors).where(authors.c.name == "a"),
        sa.select(authors).where(authors.c.name == sa.bindparam("other")),
    )
    assert sorted(raw(Author, stmt, {"other": "b"}), key=lambda x: x.name) == [a, b]


def test_spelled_and_long_column_names():
    model = OracleSpelledNameModelWithALongName
    created = model.objects.create(cased="c", a_column_name_longer_than_thirty_characters=7)
    (got,) = raw(model, sa.select(table_for(model)))
    assert got == created and got.cased == "c"
    if connection.vendor == "oracle":
        # Django's raw() names a column as the cursor reports it, lower case: Django's
        # Oracle tables cut a name past 30 characters with a hash, which no field's
        # column matches, so the field is deferred (as with Model.objects.raw() itself).
        long_name = connection.introspection.identifier_converter(
            connection.ops.quote_name("a_column_name_longer_than_thirty_characters").strip('"')
        )
        assert got.get_deferred_fields() == {"a_column_name_longer_than_thirty_characters"}
        assert getattr(got, long_name) == 7
    else:
        assert got.get_deferred_fields() == set()
    assert got.a_column_name_longer_than_thirty_characters == 7


# --- refused before anything runs -------------------------------------------------


def test_a_statement_without_the_primary_key_is_refused_before_running():
    make_author()
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError, match=r"primary key.*'id'"):
            raw(Author, sa.select(authors.c.name, authors.c.joined_at))
        with pytest.raises(ValueError, match=r"primary key.*'uid'"):
            raw(Specimen, sa.select(specimens.c.label_text))
    assert len(queries) == 0
    # A label naming the primary key's column counts.
    (got,) = raw(Author, sa.select(authors.c.id.label("id"), authors.c.name))
    assert got.name == "a"


@pytest.mark.parametrize(
    "stmt",
    [
        sa.insert(authors).values(name="n", joined_at=AT),
        sa.insert(authors).values(name="n", joined_at=AT).returning(*authors.c),
        sa.update(authors).values(name="n"),
        sa.update(authors).values(name="n").returning(*authors.c),
        sa.delete(authors),
        sa.delete(authors).returning(*authors.c),
    ],
    ids=["insert", "insert-returning", "update", "update-returning", "delete", "delete-returning"],
)
def test_dml_is_refused(stmt):
    make_author()
    with CaptureQueriesContext(connection) as queries, pytest.raises(ValueError, match="SELECT"):
        raw(Author, stmt)
    assert len(queries) == 0
    assert Author.objects.get().name == "a"


@pytest.mark.parametrize("params", [[{"name": "a"}], ({"name": "a"},), ("a",)])
def test_a_sequence_of_params_is_refused(params):
    with pytest.raises(ValueError, match="single mapping"):
        raw(Author, sa.select(authors).where(authors.c.name == sa.bindparam("name")), params)


# --- Django's RawQuerySet, as it is ----------------------------------------------


@pytest.mark.django_db(databases=["default", "other"])
def test_using_another_database():
    make_author("here")
    there = make_author("there", using="other")
    stmt = sa.select(authors)
    (got,) = raw(Author, stmt, using="other")
    assert got.name == "there" and got.pk == there.pk and got._state.db == "other"
    assert got.joined_at == AT
    assert [a.name for a in raw(Author, stmt)] == ["here"]


@pytest.mark.django_db(databases=["default", "other"])
def test_using_on_the_raw_queryset_keeps_the_sql_and_params_of_raw():
    # raw() compiles and converts for its own `using`; Django's
    # RawQuerySet.using() only moves the same SQL and params to another
    # connection. "other" is SQLite under TIME_ZONE America/New_York, so an
    # aware datetime converts there to a different value than on "default".
    assert connections["other"].timezone_name == "America/New_York"
    there = make_author("there", using="other")
    stmt = sa.select(authors).where(authors.c.joined_at == sa.bindparam("at"))
    here, moved = raw(Author, stmt, {"at": AT}), raw(Author, stmt, {"at": AT}).using("other")
    converted_there = raw(Author, stmt, {"at": AT}, using="other")
    assert moved.raw_query == here.raw_query and moved.params == here.params
    assert moved.params != converted_there.params
    assert [a.pk for a in converted_there] == [there.pk]
    if connection.vendor == "sqlite":
        # The UTC value "default" gives matches none of New York's naive times.
        assert list(moved) == []


def test_prefetch_related():
    author = make_author()
    first, second = make_book(author, title="1"), make_book(author, title="2")
    stmt = sa.select(authors)

    instances = list(raw(Author, stmt))
    with CaptureQueriesContext(connection) as queries:
        prefetch_related_objects(instances, "books")
        assert sorted(instances[0].books.all(), key=lambda b: b.title) == [first, second]
    assert len(queries) == 1

    with CaptureQueriesContext(connection) as queries:
        (got,) = raw(Author, stmt).prefetch_related("books")
        assert len(got.books.all()) == 2
    assert len(queries) == 2


def test_async_for():
    make_author("x")
    qs = raw(Author, sa.select(authors))

    async def names():
        return [author.name async for author in qs]

    # async_to_sync runs the thread-sensitive fetch on this test thread, in its transaction.
    assert async_to_sync(names)() == ["x"]


def test_raw_called_from_async_code():
    make_author("x")
    stmt = sa.select(authors)

    async def via_sync_to_async():
        return [author.name async for author in await sync_to_async(raw)(Author, stmt)]

    async def direct():
        return [author.name async for author in raw(Author, stmt)]

    assert async_to_sync(via_sync_to_async)() == ["x"]
    if connection.vendor == "mysql":
        # raw() compiles now, and MySQL's dialect depends on the server (MariaDB or not,
        # sql_mode), which the first call on a connection reads from it.
        with pytest.raises(SynchronousOnlyOperation):
            async_to_sync(direct)()
    else:
        assert async_to_sync(direct)() == ["x"]


@pytest.fixture
def compiles(monkeypatch):
    """The statements compiled so far, one entry per compilation."""
    compiled = []

    def counting_init(self, dialect, statement, *args, **kwargs):
        compiled.append(statement)
        super(_ReturningCompiler, self).__init__(dialect, statement, *args, **kwargs)

    monkeypatch.setattr(_ReturningCompiler, "__init__", counting_init, raising=False)
    return compiled


def test_a_statement_used_again_is_not_compiled_again(compiles):
    x, y = make_author("x"), make_author("y")
    stmt = sa.select(authors).where(authors.c.name == sa.bindparam("name"))
    assert list(raw(Author, stmt, {"name": "x"})) == [x]
    assert list(raw(Author, stmt, {"name": "y"})) == [y]
    assert execute(sa.select(sa.func.count()).select_from(stmt.subquery()), {"name": "x"}) == [(1,)]
    assert execute(stmt, {"name": "y"})[0][0] == y.pk
    assert len(compiles) == 2  # stmt once, the count once


def test_queries_show_up_in_execute_wrapper():
    make_author()
    seen = []

    def wrapper(execute_, sql, params, many, context):
        seen.append(sql)
        return execute_(sql, params, many, context)

    qs = raw(Author, sa.select(authors))
    with connections["default"].execute_wrapper(wrapper):
        list(qs)
    assert len(seen) == 1 and seen[0] == qs.raw_query
