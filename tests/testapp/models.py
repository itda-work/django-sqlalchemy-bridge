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
