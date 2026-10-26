import datetime
import uuid
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import connection

import django_sqlalchemy_bridge
from tests import dbenv
from tests.testapp.models import Author, Book

EXPECTED_VENDOR = {"sqlite": "sqlite", "postgres": "postgresql", "mysql": "mysql", "oracle": "oracle"}[dbenv.BACKEND]


def test_package_imports():
    assert isinstance(django_sqlalchemy_bridge.__version__, str)


@pytest.mark.django_db
def test_connects_to_the_selected_backend():
    assert connection.vendor == EXPECTED_VENDOR
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM DUAL" if connection.vendor == "oracle" else "SELECT 1")
        assert cursor.fetchone() == (1,)


@pytest.mark.skipif(dbenv.BACKEND != "postgres", reason="PostgreSQL connection options")
@pytest.mark.django_db
def test_postgresql_binding_follows_dsb_pg_server_side_binding():
    # Class names, so psycopg need not be imported: Django's default cursor is its
    # own "Cursor", a psycopg ClientCursor; ServerBindingCursor is not one.
    with connection.cursor() as cursor:
        cursor_classes = [cls.__name__ for cls in type(cursor.cursor).__mro__]
    if dbenv.PG_SERVER_SIDE_BINDING:
        assert cursor_classes[0] == "ServerBindingCursor"
        assert "ClientCursor" not in cursor_classes
        assert connection.connection.prepare_threshold == 5
    else:
        assert "ClientCursor" in cursor_classes
        assert connection.connection.prepare_threshold is None


@pytest.mark.django_db
def test_test_models_round_trip():
    author = Author.objects.create(name="a", joined_at=datetime.datetime(2026, 10, 25, 9, tzinfo=datetime.UTC))
    uid = uuid.uuid4()
    Book.objects.create(uid=uid, author=author, title="t", price=Decimal("12.50"), meta={"k": [1, 2]})

    book = Book.objects.get(uid=uid)
    assert book.price == Decimal("12.50")
    assert book.meta == {"k": [1, 2]}
    assert book.status == Book.Status.DRAFT
    assert book.author.joined_at == author.joined_at


@pytest.mark.django_db
def test_migrations_match_models():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
