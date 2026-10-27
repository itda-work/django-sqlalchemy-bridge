import uuid

from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField()


class Book(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PUBLISHED = "published"

    uid = models.UUIDField(default=uuid.uuid4, unique=True)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    title = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    meta = models.JSONField(default=dict)


class Gadget(models.Model):
    """32-bit auto PK and a natural key other models point at with ``to_field``."""

    id = models.AutoField(primary_key=True)
    code = models.CharField(max_length=12, unique=True)


class Specimen(models.Model):
    """One column of each supported kind, under a custom table and column name."""

    class Level(models.IntegerChoices):
        LOW = 1
        HIGH = 2

    uid = models.UUIDField(primary_key=True, default=uuid.uuid4)
    label = models.CharField(max_length=50, db_column="label_text")
    body = models.TextField(blank=True)
    slug = models.SlugField()
    email = models.EmailField(null=True)
    small = models.SmallIntegerField()
    positive = models.PositiveIntegerField()
    big = models.BigIntegerField()
    flag = models.BooleanField(default=False)
    maybe = models.BooleanField(null=True)
    ratio = models.FloatField()
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    day = models.DateField()
    at = models.TimeField()
    happened_at = models.DateTimeField(null=True)
    took = models.DurationField()
    blob = models.BinaryField()
    data = models.JSONField(null=True)
    level = models.IntegerField(choices=Level.choices, default=Level.LOW)
    author = models.ForeignKey(Author, on_delete=models.PROTECT, db_column="writer")
    gadget = models.ForeignKey(Gadget, on_delete=models.SET_NULL, null=True, to_field="code")
    twin = models.OneToOneField("self", on_delete=models.SET_NULL, null=True)
    tags = models.ManyToManyField(Gadget, related_name="specimens")

    class Meta:
        db_table = "dsb_specimen"


class Stamp(models.Model):
    """Nullable columns a hand-built ``sa.Table`` gives SQLAlchemy defaults to, and a ``db_default``."""

    name = models.CharField(max_length=50, null=True)
    note = models.CharField(max_length=50, null=True)
    at = models.DateTimeField(null=True)
    kind = models.CharField(max_length=10, db_default="db")


# The models below back tests/test_examples.py: a small shop whose queries are hard to write with the ORM.


class Category(models.Model):
    name = models.CharField(max_length=50)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, related_name="children")


class Product(models.Model):
    sku = models.CharField(max_length=20, unique=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    price = models.DecimalField(max_digits=10, decimal_places=2)


class Sale(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="sales")
    sold_at = models.DateTimeField()
    quantity = models.IntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)


class DailyStock(models.Model):
    """One row per product and day, written by upserts."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    day = models.DateField()
    quantity = models.IntegerField()
    counted_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product", "day"], name="dailystock_product_day")]


class PriceChange(models.Model):
    """A staged price list, applied to Product with UPDATE ... FROM."""

    sku = models.CharField(max_length=20, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)


class SaleArchive(models.Model):
    """Old sales moved out of Sale, keeping their ids (tests/comparisons.py)."""

    id = models.BigIntegerField(primary_key=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="+")
    sold_at = models.DateTimeField()
    quantity = models.IntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)


class OracleSpelledNameModelWithALongName(models.Model):
    """Names Django's Oracle backend respells: a mixed case db_column, and a table and a column past 30 characters."""

    cased = models.CharField(max_length=20, db_column="MixedCase")
    a_column_name_longer_than_thirty_characters = models.IntegerField()


class Counter(models.Model):
    """Nothing but an auto primary key: a row of it gives no column."""


class Defaulted(models.Model):
    """An auto primary key and a ``db_default``: every column may be left to the database."""

    label = models.CharField(max_length=20, db_default="dflt")


class DefaultKey(models.Model):
    """A primary key the database fills from a ``db_default``, not an auto-increment."""

    code = models.CharField(primary_key=True, max_length=10, db_default="k")
