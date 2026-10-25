from django.db import models


class Item(models.Model):
    """The rows every scenario reads, updates or reports on."""

    label = models.CharField(max_length=50)
    amount = models.IntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField()
    flag = models.BooleanField()
    customer_id = models.IntegerField(db_index=True)


class NewItem(models.Model):
    """Same columns as :class:`Item`, kept empty for the INSERT scenario."""

    label = models.CharField(max_length=50)
    amount = models.IntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField()
    flag = models.BooleanField()
    customer_id = models.IntegerField(db_index=True)
