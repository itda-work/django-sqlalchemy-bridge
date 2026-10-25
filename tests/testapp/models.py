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
