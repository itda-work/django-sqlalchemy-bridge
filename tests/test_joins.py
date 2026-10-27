"""``execute_in`` splits a long ``IN`` list into chunks; ``join_across`` joins two statements in Python (#79).

Tables in different Django databases cannot meet in one statement. ``join_across``
runs the outer statement, looks the inner one up by the outer keys with
``execute_in`` (on another database if asked) and pairs the rows by Python
equality of the converted key values.
"""

import contextlib
import datetime
import math
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import execute, execute_in, join_across, table_for
from tests.testapp.models import Author, Book, Category, Gadget, PriceChange, Product, Specimen

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
AT = datetime.datetime(2026, 10, 27, 12, 0, 0, 250000, tzinfo=SEOUL)

products = table_for(Product)
price_changes = table_for(PriceChange)
authors = table_for(Author)
books = table_for(Book)
specimens = table_for(Specimen)


@contextlib.contextmanager
def statements(using="default"):
    """Capture each statement's SQL and parameters as the cursor gets them."""
    captured = []

    def wrapper(execute, sql, params, many, context):
        captured.append((sql, tuple(params or ())))
        return execute(sql, params, many, context)

    with connections[using].execute_wrapper(wrapper):
        yield captured


def make_products(*skus):
    category = Category.objects.create(name="c")
    return [
        Product.objects.create(sku=sku, category=category, price=Decimal(n + 1) + Decimal("0.50"))
        for n, sku in enumerate(skus)
    ]


def make_prices(prices, using="default"):
    for sku, price in prices.items():
        PriceChange.objects.using(using).create(sku=sku, price=Decimal(price))


def make_specimen(**values):
    author = values.pop("author", None) or Author.objects.create(name="s", joined_at=AT)
    defaults = {
        "uid": uuid.uuid4(),
        "label": "x",
        "slug": "x",
        "small": 0,
        "positive": 0,
        "big": 0,
        "ratio": 0.5,
        "amount": Decimal("0"),
        "day": datetime.date(2026, 10, 27),
        "at": datetime.time(12),
        "took": datetime.timedelta(0),
        "blob": b"",
    }
    return Specimen.objects.create(author=author, **(defaults | values))


# execute_in


def test_execute_in_splits_the_values_into_chunks():
    make_prices({"A": "1", "B": "2", "C": "3", "D": "4", "E": "5", "F": "6"})
    stmt = sa.select(price_changes.c.sku, price_changes.c.price)
    values = ["A", "B", "C", "D", "E"]

    with CaptureQueriesContext(connection) as whole:
        expected = execute_in(stmt, price_changes.c.sku, values)
    with CaptureQueriesContext(connection) as split:
        rows = execute_in(stmt, price_changes.c.sku, values, chunk_size=2)

    assert len(whole) == 1
    assert len(split) == math.ceil(len(values) / 2) == 3
    assert sorted(rows) == sorted(expected)
    assert sorted(rows) == [("A", Decimal("1.00")), ("B", Decimal("2.00")), ("C", Decimal("3.00")),
                            ("D", Decimal("4.00")), ("E", Decimal("5.00"))]  # fmt: skip


def test_execute_in_drops_none_and_duplicate_values():
    make_prices({"A": "1", "B": "2"})
    stmt = sa.select(price_changes.c.sku)

    with statements() as captured:
        rows = execute_in(stmt, price_changes.c.sku, ["A", "A", None, "B", "B", "Z"])

    assert sorted(rows) == [("A",), ("B",)]
    assert len(captured) == 1
    assert len(captured[0][1]) == 3  # A, B, Z once each; no NULL


def test_execute_in_with_no_values_runs_nothing():
    stmt = sa.select(price_changes.c.sku)
    with CaptureQueriesContext(connection) as queries:
        assert execute_in(stmt, price_changes.c.sku, []) == []
        assert execute_in(stmt, price_changes.c.sku, [None, None]) == []
        assert execute_in(stmt, price_changes.c.sku, iter(())) == []
    assert len(queries) == 0


def test_order_by_holds_within_a_chunk_only():
    """The contract: each chunk is its own statement, so ORDER BY (and LIMIT) apply per chunk."""
    make_prices({"A": "1", "B": "2", "C": "3", "D": "4"})
    stmt = sa.select(price_changes.c.sku).order_by(price_changes.c.sku.desc())

    whole = execute_in(stmt, price_changes.c.sku, ["A", "B", "C", "D"])
    split = execute_in(stmt, price_changes.c.sku, ["A", "B", "C", "D"], chunk_size=2)

    assert whole == [("D",), ("C",), ("B",), ("A",)]
    assert split == [("B",), ("A",), ("D",), ("C",)]  # the chunks [A, B] and [C, D], each sorted


def test_execute_in_keeps_the_statements_own_conditions_and_converts_results():
    make_prices({"A": "1", "B": "20", "C": "3"})
    stmt = sa.select(price_changes.c.sku, price_changes.c.price).where(price_changes.c.price < Decimal("10"))
    rows = execute_in(stmt, price_changes.c.sku, ["A", "B", "C"], chunk_size=1)
    assert sorted(rows) == [("A", Decimal("1.00")), ("C", Decimal("3.00"))]


def test_execute_in_refuses_what_it_cannot_run():
    stmt = sa.select(price_changes.c.sku)
    with pytest.raises(TypeError, match="Select"):
        execute_in(sa.text("SELECT 1"), price_changes.c.sku, ["A"])
    with pytest.raises(TypeError, match="Select"):
        execute_in(sa.union_all(stmt, stmt), price_changes.c.sku, ["A"])
    with pytest.raises(TypeError, match="column"):
        execute_in(stmt, "sku", ["A"])
    for size in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="chunk_size"):
            execute_in(stmt, price_changes.c.sku, ["A"], chunk_size=size)


def test_the_default_chunk_respects_the_database_limits():
    """The smaller of ops.max_in_list_size() and features.max_query_params less the statement's own, else 1000."""
    in_list = connection.ops.max_in_list_size()
    params = connection.features.max_query_params
    limits = [limit for limit in (in_list, params - 1 if params else None) if limit]
    size = min(limits, default=1000)
    make_prices({"SHIRT": "1"})
    stmt = sa.select(price_changes.c.sku).where(price_changes.c.price > Decimal("0"))  # one parameter of its own
    values = [f"X{i}" for i in range(size)] + ["SHIRT"]  # one more than a chunk

    with statements() as captured:
        rows = execute_in(stmt, price_changes.c.sku, values)

    assert rows == [("SHIRT",)]
    assert [len(params) for _, params in captured] == [size + 1, 2]


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's variable limit")
def test_more_values_than_sqlite_takes_variables():
    limit = connection.features.max_query_params
    make_prices({"SHIRT": "1", "TEA": "2"})
    values = [f"X{i}" for i in range(limit + 234)] + ["TEA", "SHIRT"]

    with CaptureQueriesContext(connection) as queries:
        rows = execute_in(sa.select(price_changes.c.sku), price_changes.c.sku, values)

    assert sorted(rows) == [("SHIRT",), ("TEA",)]
    assert len(queries) == 2


@pytest.mark.skipif(connection.vendor != "oracle", reason="Oracle's IN list limit (ORA-01795)")
def test_more_values_than_an_oracle_in_list_takes():
    make_prices({"SHIRT": "1"})
    values = [f"X{i}" for i in range(1000)] + ["SHIRT"]

    with CaptureQueriesContext(connection) as queries:
        rows = execute_in(sa.select(price_changes.c.sku), price_changes.c.sku, values)

    assert rows == [("SHIRT",)]
    assert len(queries) == 2


# join_across


def test_join_across_pairs_the_rows_in_outer_order():
    make_products("JEANS", "SHIRT", "TEA")
    make_prices({"SHIRT": "27.50", "TEA": "8.50", "GONE": "1.00"})
    outer = sa.select(products.c.sku, products.c.price).order_by(products.c.sku)
    inner = sa.select(price_changes.c.sku, price_changes.c.price)

    with CaptureQueriesContext(connection) as queries:
        inner_rows = join_across(outer, inner, (products.c.sku, price_changes.c.sku))
    left_rows = join_across(outer, inner, (products.c.sku, price_changes.c.sku), how="left")

    assert len(queries) == 2
    assert inner_rows == [
        ("SHIRT", Decimal("2.50"), "SHIRT", Decimal("27.50")),
        ("TEA", Decimal("3.50"), "TEA", Decimal("8.50")),
    ]
    assert left_rows == [
        ("JEANS", Decimal("1.50"), None, None),
        ("SHIRT", Decimal("2.50"), "SHIRT", Decimal("27.50")),
        ("TEA", Decimal("3.50"), "TEA", Decimal("8.50")),
    ]


def test_join_across_splits_the_inner_lookup_into_chunks():
    make_products("A", "B", "C", "D", "E")
    make_prices({"A": "1", "C": "3", "E": "5"})
    outer = sa.select(products.c.sku).order_by(products.c.sku)
    inner = sa.select(price_changes.c.price, price_changes.c.sku)
    on = (products.c.sku, price_changes.c.sku)

    whole = join_across(outer, inner, on)
    with CaptureQueriesContext(connection) as queries:
        split = join_across(outer, inner, on, chunk_size=2)

    assert len(queries) == 1 + math.ceil(5 / 2)
    assert split == whole == [("A", Decimal("1.00"), "A"), ("C", Decimal("3.00"), "C"), ("E", Decimal("5.00"), "E")]


def test_one_outer_row_gets_every_inner_row_it_matches():
    """1:N: an outer row repeats for each inner row, in fetch order, and the outer order holds."""
    author_b = Author.objects.create(name="b", joined_at=AT)
    author_a = Author.objects.create(name="a", joined_at=AT)
    Author.objects.create(name="c", joined_at=AT)  # no books
    for author, title, price in [(author_a, "a1", "1"), (author_b, "b1", "2"), (author_a, "a2", "3")]:
        Book.objects.create(author=author, title=title, price=Decimal(price))
    outer = sa.select(authors.c.name, authors.c.id).order_by(authors.c.name)
    inner = sa.select(books.c.title, books.c.author_id).order_by(books.c.title)

    rows = join_across(outer, inner, (authors.c.id, books.c.author_id))
    left = join_across(outer, inner, (authors.c.id, books.c.author_id), how="left")

    assert [(name, title) for name, _, title, _ in rows] == [("a", "a1"), ("a", "a2"), ("b", "b1")]
    assert [(name, title) for name, _, title, _ in left] == [("a", "a1"), ("a", "a2"), ("b", "b1"), ("c", None)]


def test_outer_rows_with_a_null_key_match_nothing():
    Gadget.objects.create(code="g1")
    make_specimen(label="with", gadget_id="g1")
    make_specimen(label="without", gadget_id=None)
    outer = sa.select(specimens.c.label_text, specimens.c.gadget_id).order_by(specimens.c.label_text)
    gadgets = table_for(Gadget)
    inner = sa.select(gadgets.c.code)

    with statements() as captured:
        rows = join_across(outer, inner, (specimens.c.gadget_id, gadgets.c.code))
    left = join_across(outer, inner, (specimens.c.gadget_id, gadgets.c.code), how="left")

    assert rows == [("with", "g1", "g1")]
    assert left == [("with", "g1", "g1"), ("without", None, None)]
    assert len(captured[1][1]) == 1  # the inner IN holds g1 only


def test_duplicate_outer_keys_go_into_the_in_list_once():
    make_products("A", "B")
    make_prices({"A": "1", "B": "2"})
    both = sa.union_all(sa.select(products.c.sku), sa.select(products.c.sku)).subquery()
    outer = sa.select(both.c.sku).order_by(both.c.sku)
    inner = sa.select(price_changes.c.sku, price_changes.c.price)

    with statements() as captured:
        rows = join_across(outer, inner, (both.c.sku, price_changes.c.sku))

    assert [sku for sku, *_ in rows] == ["A", "A", "B", "B"]
    assert len(captured) == 2
    assert len(captured[1][1]) == 2


def test_how_is_inner_or_left():
    stmt = sa.select(products.c.sku)
    with CaptureQueriesContext(connection) as queries:
        for how in ("right", "outer", "full", "LEFT", None):
            with pytest.raises(ValueError, match="how"):
                join_across(stmt, stmt, (products.c.sku, products.c.sku), how=how)
    assert len(queries) == 0


def test_the_keys_are_found_by_column_label_or_name():
    make_products("SHIRT", "TEA")
    make_prices({"SHIRT": "27.50", "TEA": "8.50"})
    sku_label = products.c.sku.label("code")
    outer = sa.select(sku_label).order_by(products.c.sku)
    inner = sa.select(price_changes.c.price, price_changes.c.sku.label("p_sku"))
    expected = [("SHIRT", Decimal("27.50"), "SHIRT"), ("TEA", Decimal("8.50"), "TEA")]

    assert join_across(outer, inner, (sku_label, price_changes.c.sku.label("p_sku"))) == expected  # labels
    assert join_across(outer, inner, (products.c.sku.label("code"), price_changes.c.sku)) == expected  # equal ones
    assert join_across(outer, inner, ("code", "p_sku")) == expected  # result key names
    assert join_across(outer, inner, (products.c.sku, "p_sku")) == expected  # the labelled column


def test_a_key_not_selected_is_refused_before_running():
    stmt_outer = sa.select(products.c.sku)
    stmt_inner = sa.select(price_changes.c.price)
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError, match="inner"):
            join_across(stmt_outer, stmt_inner, (products.c.sku, price_changes.c.sku))
        with pytest.raises(ValueError, match="outer"):
            join_across(stmt_outer, stmt_inner, (products.c.price, price_changes.c.price))
        with pytest.raises(ValueError, match="outer"):
            join_across(stmt_outer, stmt_inner, ("nope", price_changes.c.price))
        with pytest.raises(ValueError, match="on"):
            join_across(stmt_outer, stmt_inner, (products.c.sku,))
    assert len(queries) == 0


def test_composite_keys_are_not_supported():
    stmt = sa.select(products.c.sku, products.c.price)
    with pytest.raises(NotImplementedError):
        join_across(stmt, stmt, ((products.c.sku, products.c.price), (products.c.sku, products.c.price)))
    with pytest.raises(NotImplementedError):
        join_across(stmt, stmt, (sa.tuple_(products.c.sku, products.c.price), products.c.sku))


def test_join_across_takes_select_statements_only():
    stmt = sa.select(products.c.sku)
    on = (products.c.sku, products.c.sku)
    with pytest.raises(TypeError, match="Select"):
        join_across(sa.text("SELECT sku FROM t"), stmt, on)
    with pytest.raises(TypeError, match="Select"):
        join_across(stmt, sa.union_all(stmt, stmt), on)
    with pytest.raises(TypeError, match="Select"):
        join_across(stmt, products, on)


# Key values: the outer key comes back through its Django field, the inner IN converts it through the inner one.


def test_aware_datetime_keys_match_the_same_instant_in_another_zone():
    early = Author.objects.create(name="early", joined_at=AT)
    Author.objects.create(name="late", joined_at=AT + datetime.timedelta(seconds=1))
    make_specimen(label="same instant", happened_at=AT.astimezone(datetime.UTC), author=early)
    make_specimen(label="other", happened_at=AT - datetime.timedelta(hours=9), author=early)
    outer = sa.select(authors.c.name, authors.c.joined_at).order_by(authors.c.name)
    inner = sa.select(specimens.c.label_text, specimens.c.happened_at)

    rows = join_across(outer, inner, (authors.c.joined_at, specimens.c.happened_at), how="left")

    assert [(name, label) for name, _, label, _ in rows] == [("early", "same instant"), ("late", None)]
    assert rows[0][1] == rows[0][3] == AT
    los_angeles = AT.astimezone(ZoneInfo("America/Los_Angeles"))
    assert execute_in(sa.select(specimens.c.label_text), specimens.c.happened_at, [los_angeles]) == [("same instant",)]


def test_decimal_keys_match_across_scales():
    make_products("A", "B")  # prices 1.50 and 2.50 (decimal_places=2)
    make_specimen(label="one and a half", amount=Decimal("1.5"))  # decimal_places=3
    outer = sa.select(products.c.sku, products.c.price).order_by(products.c.sku)
    inner = sa.select(specimens.c.label_text, specimens.c.amount)

    rows = join_across(outer, inner, (products.c.price, specimens.c.amount), how="left")

    assert [(sku, label) for sku, _, label, _ in rows] == [("A", "one and a half"), ("B", None)]
    assert rows[0][3] == Decimal("1.500")
    assert execute_in(sa.select(specimens.c.label_text), specimens.c.amount, [Decimal("1.50")]) == [("one and a half",)]


def test_uuid_keys_match():
    author = Author.objects.create(name="a", joined_at=AT)
    book = Book.objects.create(author=author, title="t", price=Decimal("1"))
    Book.objects.create(author=author, title="u", price=Decimal("1"))
    make_specimen(label="twin", uid=book.uid, author=author)
    outer = sa.select(books.c.title, books.c.uid).order_by(books.c.title)
    inner = sa.select(specimens.c.label_text, specimens.c.uid)

    rows = join_across(outer, inner, (books.c.uid, specimens.c.uid))

    assert rows == [("t", book.uid, "twin", book.uid)]
    assert execute_in(sa.select(specimens.c.label_text), specimens.c.uid, [str(book.uid)]) == [("twin",)]


def test_keys_of_different_field_types():
    """The outer key is a CharField value; the inner column a TextField (NCLOB on Oracle, not comparable there)."""
    if connection.vendor == "oracle":
        pytest.skip("Oracle's TextField is NCLOB, which IN cannot compare (ORA-22848)")
    make_products("SHIRT", "TEA")
    make_specimen(label="shirt", body="SHIRT")
    outer = sa.select(products.c.sku).order_by(products.c.sku)
    inner = sa.select(specimens.c.label_text, specimens.c.body)

    rows = join_across(outer, inner, (products.c.sku, specimens.c.body), how="left")

    assert rows == [("SHIRT", "shirt", "SHIRT"), ("TEA", None, None)]


def test_keys_through_a_foreign_key_to_a_natural_key():
    """CharField outer key, inner ForeignKey(to_field="code") column: converted through its target field."""
    gadgets = table_for(Gadget)
    Gadget.objects.create(code="g1")
    Gadget.objects.create(code="g2")
    make_specimen(label="s", gadget_id="g2")
    outer = sa.select(gadgets.c.code).order_by(gadgets.c.code)
    inner = sa.select(specimens.c.label_text, specimens.c.gadget_id)

    assert join_across(outer, inner, (gadgets.c.code, specimens.c.gadget_id)) == [("g2", "s", "g2")]


# Across databases


@pytest.mark.django_db(databases=["default", "other"])
def test_join_across_two_databases():
    make_products("JEANS", "SHIRT", "TEA", "CAP")
    make_prices({"SHIRT": "27.50", "TEA": "8.50", "CAP": "3.00", "GONE": "1.00"}, using="other")
    make_prices({"JEANS": "99.00"})  # in default only: must not be found
    outer = sa.select(products.c.sku).order_by(products.c.sku)
    inner = sa.select(price_changes.c.sku, price_changes.c.price)

    with CaptureQueriesContext(connection) as here, CaptureQueriesContext(connections["other"]) as there:
        rows = join_across(
            outer, inner, (products.c.sku, price_changes.c.sku), inner_using="other", how="left", chunk_size=2
        )

    assert rows == [
        ("CAP", "CAP", Decimal("3.00")),
        ("JEANS", None, None),
        ("SHIRT", "SHIRT", Decimal("27.50")),
        ("TEA", "TEA", Decimal("8.50")),
    ]
    assert len(here) == 1
    assert len(there) == 2


@pytest.mark.django_db(databases=["default", "other"])
def test_using_picks_the_outer_database_and_the_inner_follows_it():
    make_products("SHIRT")
    category = Category.objects.using("other").create(name="c")
    Product.objects.using("other").create(sku="TEA", category=category, price=Decimal("1"))
    make_prices({"TEA": "8.50"}, using="other")
    make_prices({"SHIRT": "27.50"})
    outer = sa.select(products.c.sku)
    inner = sa.select(price_changes.c.price, price_changes.c.sku)

    with CaptureQueriesContext(connection) as here, CaptureQueriesContext(connections["other"]) as there:
        rows = join_across(outer, inner, (products.c.sku, price_changes.c.sku), using="other")

    assert rows == [("TEA", Decimal("8.50"), "TEA")]
    assert (len(here), len(there)) == (0, 2)
    assert execute(sa.select(products.c.sku)) == [("SHIRT",)]


def test_the_chunks_share_one_compilation(monkeypatch):
    """Every chunk runs the same statement with an expanding parameter: it compiles once per call."""
    make_prices({"A": "1", "B": "2", "C": "3"})
    compiled = []
    compile_ = sa.Select.compile

    def counting(self, *args, **kwargs):
        compiled.append(self)
        return compile_(self, *args, **kwargs)

    monkeypatch.setattr(sa.Select, "compile", counting)
    stmt = sa.select(price_changes.c.sku).where(price_changes.c.price > Decimal("0"))
    rows = execute_in(stmt, price_changes.c.sku, ["A", "B", "C"], chunk_size=1)

    assert sorted(rows) == [("A",), ("B",), ("C",)]
    assert len(compiled) == 1


@pytest.mark.skipif(connection.vendor != "mysql", reason="MySQL's _ci collations")
def test_keys_pair_by_python_equality_not_by_collation():
    """A _ci collation finds "SHIRT" for "shirt" in the IN list; the Python pairing does not match them.

    The test database is utf8mb4_bin (tests/dbenv.py), so the collation is given in the statement.
    """
    make_prices({"SHIRT": "27.50"})
    gadgets = table_for(Gadget)
    Gadget.objects.create(code="shirt")
    sku = price_changes.c.sku.collate("utf8mb4_0900_ai_ci").label("sku")
    outer = sa.select(gadgets.c.code)
    inner = sa.select(sku)

    assert execute_in(inner, sku, ["shirt"]) == [("SHIRT",)]
    assert join_across(outer, inner, (gadgets.c.code, sku)) == []
    assert join_across(outer, inner, (gadgets.c.code, sku), how="left") == [("shirt", None)]
