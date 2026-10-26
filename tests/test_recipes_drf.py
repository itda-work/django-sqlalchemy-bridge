"""Recipes for Django REST framework and class-based views, written with the bridge.

The views call the bridge where a QuerySet would normally go and are called
directly with ``APIRequestFactory`` (no URLconf). The code between
``# readme:NAME`` and ``# /readme:NAME`` is printed in docs/drf.md as it is
here; ``tests/test_readme_examples.py`` fails when the two differ.
"""

from decimal import Decimal

import pytest
import sqlalchemy as sa
from django.db import connection, transaction
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import serializers, status, viewsets
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from django_sqlalchemy_bridge import execute, execute_result, raw, table_for
from tests.testapp.models import Category, Product, Sale

pytestmark = pytest.mark.django_db

api = APIRequestFactory()


# readme:drf-list
products = table_for(Product)
categories = table_for(Category)
sales = table_for(Sale)

product_revenue = (
    sa.select(
        products.c.sku,
        categories.c.name.label("category"),
        sa.func.coalesce(sa.func.sum(sales.c.amount), 0).label("revenue"),  # Decimal, as the ORM reads it
    )
    .join(categories, categories.c.id == products.c.category_id)
    .outerjoin(sales, sales.c.product_id == products.c.id)
    .group_by(products.c.id, products.c.sku, categories.c.name)
    .order_by(products.c.sku)
)


class ProductRevenueSerializer(serializers.Serializer):
    sku = serializers.CharField()
    category = serializers.CharField()
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2)


class ProductRevenueViewSet(viewsets.ViewSet):
    def list(self, request):
        rows = execute_result(product_revenue).mappings().all()
        return Response(ProductRevenueSerializer(rows, many=True).data)


# /readme:drf-list


# readme:drf-pagination
class StatementPagination(LimitOffsetPagination):
    """LimitOffsetPagination over a SQLAlchemy SELECT instead of a QuerySet."""

    default_limit = 20

    def paginate_queryset(self, stmt, request, view=None):
        self.request = request
        self.limit = self.get_limit(request)
        self.offset = self.get_offset(request)
        self.count = execute(sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery()))[0][0]
        return execute_result(stmt.limit(self.limit).offset(self.offset)).mappings().all()


class ProductRevenuePageView(APIView):
    def get(self, request):
        paginator = StatementPagination()
        rows = paginator.paginate_queryset(product_revenue, request, view=self)
        return paginator.get_paginated_response(ProductRevenueSerializer(rows, many=True).data)


# /readme:drf-pagination


# readme:drf-create
class ProductSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    sku = serializers.CharField(max_length=20)
    category_id = serializers.IntegerField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)


create_product = sa.insert(products).returning(products.c.id, products.c.sku, products.c.category_id, products.c.price)


class ProductCreateView(APIView):
    def post(self, request):
        serializer = ProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # The params of a bare insert() are the column values; no save(), signals or field defaults
        row = execute_result(create_product, serializer.validated_data).mappings().one()
        return Response(ProductSerializer(row).data, status=status.HTTP_201_CREATED)


# /readme:drf-create


# readme:drf-atomic
sold_quantity = sa.select(sa.func.sum(sales.c.quantity)).where(sales.c.product_id == sa.bindparam("product"))


class SaleSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1)


class SaleCreateView(APIView):
    def post(self, request, pk):
        serializer = SaleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        quantity = serializer.validated_data["quantity"]
        with transaction.atomic():  # with ATOMIC_REQUESTS the whole request is such a block
            product = Product.objects.get(pk=pk)
            Sale.objects.create(
                product=product, sold_at=timezone.now(), quantity=quantity, amount=product.price * quantity
            )
            sold = execute(sold_quantity, {"product": pk})[0][0]  # sees the row the ORM just wrote
        return Response({"sku": product.sku, "sold": sold}, status=status.HTTP_201_CREATED)


# /readme:drf-atomic


# readme:drf-template
category_ranking = (
    sa.select(
        categories.c.name,
        sa.func.sum(sales.c.amount).label("revenue"),
        sa.func.rank().over(order_by=sa.func.sum(sales.c.amount).desc()).label("rank"),
    )
    .join(products, products.c.category_id == categories.c.id)
    .join(sales, sales.c.product_id == products.c.id)
    .group_by(categories.c.id, categories.c.name)
    .order_by("rank", categories.c.name)
)


class CategoryReportView(TemplateView):
    template_name = "shop/category_report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ranking"] = execute_result(category_ranking).mappings().all()
        return context


# /readme:drf-template


# readme:drf-raw
top_products = (
    sa.select(products, sa.func.sum(sales.c.amount).label("revenue"))
    .join(sales, sales.c.product_id == products.c.id)
    .where(products.c.price >= sa.bindparam("min_price"))  # "10" converts as the ORM's price__gte="10"
    .group_by(*products.c)
    .order_by(sa.func.sum(sales.c.amount).desc(), products.c.sku)
)


class ProductModelSerializer(serializers.ModelSerializer):
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)  # the label, an attribute

    class Meta:
        model = Product
        fields = ["id", "sku", "category", "price", "revenue"]


class TopProductsView(APIView):
    def get(self, request):
        instances = raw(Product, top_products, {"min_price": request.query_params.get("min_price", "0")})
        return Response(ProductModelSerializer(instances, many=True).data)


class TopProductsReportView(TemplateView):
    template_name = "shop/top_products.html"  # {% for p in products %}{{ p.sku }} {{ p.category.name }}{% endfor %}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # A RawQuerySet: the query runs when the template iterates it, the categories in one more
        context["products"] = raw(Product, top_products, {"min_price": 0}).prefetch_related("category")
        return context


# /readme:drf-raw


@pytest.fixture
def shop():
    """의류 (SHIRT, JEANS, SOCKS with no sales) and 식품 (TEA)."""
    clothes = Category.objects.create(name="의류")
    food = Category.objects.create(name="식품")
    shirt = Product.objects.create(sku="SHIRT", category=clothes, price=Decimal("30.00"))
    jeans = Product.objects.create(sku="JEANS", category=clothes, price=Decimal("50.00"))
    Product.objects.create(sku="SOCKS", category=clothes, price=Decimal("5.00"))
    tea = Product.objects.create(sku="TEA", category=food, price=Decimal("8.50"))
    for product, quantity in [(shirt, 2), (jeans, 1), (tea, 4), (tea, 2)]:
        Sale.objects.create(product=product, sold_at=timezone.now(), quantity=quantity, amount=product.price * quantity)
    return {"clothes": clothes, "food": food, "tea": tea}


def test_a_viewset_list_serializes_bridge_rows(shop):
    response = ProductRevenueViewSet.as_view({"get": "list"})(api.get("/products/"))

    assert response.status_code == 200
    assert response.data == [
        {"sku": "JEANS", "category": "의류", "revenue": "50.00"},
        {"sku": "SHIRT", "category": "의류", "revenue": "60.00"},
        {"sku": "SOCKS", "category": "의류", "revenue": "0.00"},
        {"sku": "TEA", "category": "식품", "revenue": "51.00"},
    ]


def test_limit_offset_pagination_runs_a_count_and_a_sliced_statement(shop):
    view = ProductRevenuePageView.as_view()

    with CaptureQueriesContext(connection) as queries:
        first = view(api.get("/products/", {"limit": 2}))
    last = view(api.get("/products/", {"limit": 2, "offset": 2}))

    assert len(queries) == 2
    assert first.data == {
        "count": 4,
        "next": "http://testserver/products/?limit=2&offset=2",
        "previous": None,
        "results": [
            {"sku": "JEANS", "category": "의류", "revenue": "50.00"},
            {"sku": "SHIRT", "category": "의류", "revenue": "60.00"},
        ],
    }
    assert last.data == {
        "count": 4,
        "next": None,
        "previous": "http://testserver/products/?limit=2",
        "results": [
            {"sku": "SOCKS", "category": "의류", "revenue": "0.00"},
            {"sku": "TEA", "category": "식품", "revenue": "51.00"},
        ],
    }
    assert view(api.get("/products/")).data["results"][-1]["sku"] == "TEA"  # default_limit


def test_validated_data_is_inserted_with_returning(shop, needs_returning):
    data = {"sku": "CAP", "category_id": shop["clothes"].pk, "price": "12.50"}

    response = ProductCreateView.as_view()(api.post("/products/", data, format="json"))

    assert response.status_code == 201
    created = Product.objects.get(sku="CAP")
    assert response.data == {"id": created.pk, "sku": "CAP", "category_id": shop["clothes"].pk, "price": "12.50"}
    assert created.price == Decimal("12.50")

    invalid = ProductCreateView.as_view()(api.post("/products/", {"sku": "HAT", "price": "x"}, format="json"))
    assert invalid.status_code == 400
    assert set(invalid.data) == {"category_id", "price"}
    assert not Product.objects.filter(sku="HAT").exists()


def test_orm_write_and_bridge_read_share_the_request_transaction(shop):
    tea = shop["tea"]

    with CaptureQueriesContext(connection) as queries:
        response = SaleCreateView.as_view()(api.post("/sales/", {"quantity": 3}, format="json"), pk=tea.pk)

    assert response.status_code == 201
    assert response.data == {"sku": "TEA", "sold": 4 + 2 + 3}
    # Every statement ran on the one connection CaptureQueriesContext watches, in this order:
    # the savepoint atomic() opened, the ORM INSERT, the bridge SELECT, the savepoint release.
    sql = [q["sql"].upper() for q in queries]
    assert sql[0].startswith("SAVEPOINT")
    # Oracle has no RELEASE SAVEPOINT; Django logs it there as "-- RELEASE SAVEPOINT ... (FAKED)".
    assert sql[-1].removeprefix("-- ").startswith("RELEASE SAVEPOINT")
    insert = next(i for i, s in enumerate(sql) if s.startswith("INSERT INTO") and "TESTAPP_SALE" in s)
    select = next(i for i, s in enumerate(sql) if s.startswith("SELECT SUM("))
    assert insert < select


def test_template_view_context_holds_bridge_rows(shop):
    response = CategoryReportView.as_view()(RequestFactory().get("/report/"))

    assert [dict(row) for row in response.context_data["ranking"]] == [
        {"name": "의류", "revenue": Decimal("110.00"), "rank": 1},
        {"name": "식품", "revenue": Decimal("51.00"), "rank": 2},
    ]


def test_a_model_serializer_takes_the_instances_of_raw(shop):
    with CaptureQueriesContext(connection) as queries:
        response = TopProductsView.as_view()(api.get("/top/", {"min_price": "10"}))

    assert len(queries) == 1  # the category is the foreign key's value, no query per row
    assert response.status_code == 200
    clothes = shop["clothes"].pk
    shirt, jeans = Product.objects.get(sku="SHIRT").pk, Product.objects.get(sku="JEANS").pk
    assert response.data == [
        {"id": shirt, "sku": "SHIRT", "category": clothes, "price": "30.00", "revenue": "60.00"},
        {"id": jeans, "sku": "JEANS", "category": clothes, "price": "50.00", "revenue": "50.00"},
    ]


def test_a_template_iterates_the_instances_of_raw(shop):
    response = TopProductsReportView.as_view()(RequestFactory().get("/top/"))
    products = response.context_data["products"]

    with CaptureQueriesContext(connection) as queries:
        rows = [(p.sku, p.category.name) for p in products]
    assert len(queries) == 2  # the products, then their categories (prefetch_related)
    assert rows == [("SHIRT", "의류"), ("TEA", "식품"), ("JEANS", "의류")]
    assert all(isinstance(p, Product) for p in products)
