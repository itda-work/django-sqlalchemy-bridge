# DRF·클래스 기반 뷰에서 쓰기

브리지는 튜플과 SQLAlchemy `Row`를 돌려주고 QuerySet·모델 인스턴스를 만들지 않습니다. Django 제네릭 CBV 와 DRF 의 확장 지점(`get_queryset`·`get_object`·`ModelSerializer`·`filter_backends`·페이지네이션)은 QuerySet 계약 위에 있어 그대로는 못 씁니다. 이 문서는 어디까지 되고 어디서 끊기는지를 돌아가는 코드로 보여 줍니다.

아래 코드는 [`tests/test_recipes_drf.py`](../tests/test_recipes_drf.py)에 그대로 있고 SQLite·PostgreSQL·MySQL·Oracle 에서 돕니다(레시피 3 은 MySQL·Oracle 제외, 문서와 어긋나면 `tests/test_readme_examples.py`가 실패합니다). 모델은 테스트의 상점 모델 `Category(name, parent)`·`Product(sku, category, price)`·`Sale(product, sold_at, quantity, amount)`입니다. 공통 import:

```python
import sqlalchemy as sa
from django.db import transaction
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import serializers, status, viewsets
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from django_sqlalchemy_bridge import execute, execute_result, table_for
```

## 1. 목록: `ViewSet.list()`와 일반 `Serializer`

`execute_result(stmt).mappings().all()`의 행은 `Mapping`이라 일반 `Serializer`가 키로 읽습니다. 직렬화기 필드 이름을 SELECT 의 컬럼 이름·라벨과 맞추면 됩니다. 컬럼 타입을 잇는 식(`coalesce(sum(amount), 0)`)은 ORM 처럼 `Decimal`로 옵니다.

<!-- example:drf-list -->
```python
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
```
<!-- /example:drf-list -->

`APIView.get()`에서도 똑같이 씁니다.

## 2. 페이지네이션

`LimitOffsetPagination`·`PageNumberPagination`은 QuerySet 의 `count()`와 슬라이싱을 요구합니다. SQLAlchemy 문장으로는 count 문장과 `.limit().offset()` 문장을 따로 실행하고, 응답 형식(`count`·`next`·`previous`·`results`)은 DRF 의 `get_paginated_response()`를 빌립니다.

`LimitOffsetPagination`을 상속해 `paginate_queryset()`만 덮었습니다. DRF 문서가 사용자 정의 페이지네이션에 안내하는 확장 지점이고, `limit`·`offset` 파싱과 링크 생성을 DRF 에 그대로 맡기므로 뷰에서 직접 읽는 방법보다 짧습니다.

<!-- example:drf-pagination -->
```python
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
```
<!-- /example:drf-pagination -->

- count 문장은 `order_by(None)`으로 정렬을 뗀 원래 문장을 서브쿼리로 감쌉니다. 페이지가 겹치거나 빠지지 않도록 원래 문장에는 유일한 정렬을 두세요(여기서는 `sku`).
- `limit`·`offset`을 붙인 문장은 요청마다 새 객체라 매번 컴파일합니다(짧은 문장이라 비용이 작습니다).
- `PageNumberPagination`은 링크와 `count`를 Django `Page` 객체에서 읽어서 같은 방식으로 덮기 어렵습니다. LimitOffset 형식을 쓰세요.
- `CursorPagination`은 안 됩니다. 커서 조건을 QuerySet 의 `order_by()`·`filter()`로 만들기 때문입니다.

## 3. 쓰기: `validated_data`를 INSERT … RETURNING 으로

검증은 `Serializer`가, 쓰기는 `execute(insert(...).returning(...))`가 합니다. 값이 없는 `insert(table)`에 넘긴 params 가 컬럼 값이 되므로 직렬화기 필드 이름을 컬럼 이름(FK 는 `category_id`)과 맞춥니다. 문장은 모듈 수준에 한 번 만들어 두면 컴파일 캐시를 탑니다.

<!-- example:drf-create -->
```python
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
```
<!-- /example:drf-create -->

MySQL 에는 `RETURNING`이 없고 브리지는 Oracle 의 `RETURNING … INTO`를 받지 않아, 두 DB 에서 이 레시피는 `NotSupportedError`입니다. `execute(sa.insert(products), serializer.validated_data)`로 넣고 유일 키(`sku`)로 다시 SELECT 하거나 ORM 으로 쓰세요.

여러 행이면 `ProductSerializer(data=request.data, many=True)`로 검증한 뒤 `bulk_insert(products, serializer.validated_data, returning=["id"])`로 한 번에 넣습니다. README 의 경고가 여기에도 그대로 적용됩니다.

> ⚠️ **브리지로 쓴 내용은 모델 계층을 거치지 않습니다.** `save()`·`delete()`가 불리지 않고, `pre_save`/`post_save`/`pre_delete`/`post_delete` 등 **signal이 발생하지 않으며**, Django 필드의 `default`·`auto_now`도 채워지지 않습니다. 값은 모두 직접 넣어야 합니다.

signal·`save()`·모델 검증이 필요한 쓰기는 `ModelSerializer`와 ORM 으로 하세요.

## 4. ORM 과 같은 트랜잭션

브리지는 Django 커서로 실행하므로 `transaction.atomic()`이나 `ATOMIC_REQUESTS = True`(요청마다 뷰 전체를 `atomic()`으로 감쌈) 안에서 ORM 과 같은 연결·같은 트랜잭션에 들어갑니다. ORM 이 방금 쓴 행을 브리지가 읽고, 예외가 나면 둘 다 롤백됩니다.

<!-- example:drf-atomic -->
```python
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
```
<!-- /example:drf-atomic -->

테스트는 `CaptureQueriesContext(connection)` 하나에 SAVEPOINT → ORM INSERT → 브리지 SELECT → RELEASE SAVEPOINT 가 차례로 잡히는 것으로 같은 연결임을 확인합니다. (`APIRequestFactory`는 뷰를 직접 불러 `ATOMIC_REQUESTS`가 적용되지 않으므로 레시피는 `atomic()`을 직접 씁니다.)

## 5. `TemplateView.get_context_data()`

템플릿에서는 `{{ row.name }}`처럼 씁니다. 템플릿 변수 조회가 먼저 `row["name"]`을 시도하므로 매핑 행이 그대로 읽힙니다.

<!-- example:drf-template -->
```python
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
```
<!-- /example:drf-template -->

## 되지 않는 것

QuerySet 이나 모델 인스턴스를 받는 확장 지점은 브리지 결과를 넣을 수 없습니다. 브리지는 QuerySet 을 흉내 내는 어댑터를 두지 않습니다(README "설계 원칙").

| 확장 지점 | 왜 안 되나 | 대안 |
|---|---|---|
| `ListView`·`DetailView`·`UpdateView` | `get_queryset()`이 QuerySet 을, `get_object()`가 `queryset.get()`을 쓰고, `UpdateView`는 `ModelForm`이 인스턴스를 저장한다 | ORM 으로 돌아가거나, `TemplateView`·`View`에서 직접 조회(5) |
| `ModelViewSet` 기본 동작 | list·retrieve·update·destroy 가 `get_queryset()`·`get_object()`를, create·update 가 `serializer.save()`(ORM)를 거친다 | `ViewSet`에 필요한 메서드만 직접 쓰기(1·3), 또는 ORM |
| `ModelSerializer` 자동 필드 | 필드는 모델에서 만들지만 `save()`가 ORM 으로 쓰고, FK 필드(`PrimaryKeyRelatedField`)는 관련 객체를 기대해 매핑 행에서 `'int' object has no attribute 'pk'`가 난다 | 일반 `Serializer`에 필드를 적기(1·3) |
| django-filter·`OrderingFilter`·`SearchFilter` | `filter_queryset()`이 `queryset.filter()`·`order_by()`를 부른다 | 쿼리 파라미터를 읽어 문장에 `.where()`·`.order_by()`를 직접 붙이기(정렬 컬럼은 허용 목록으로) |
| `get_object()`를 거치는 객체 권한 | `get_object()`가 `check_object_permissions(request, obj)`를 부르고 권한 클래스는 모델 인스턴스를 받는다고 가정한다 | 행을 읽은 뒤 `self.check_object_permissions(request, row)`를 직접 부르고 권한 클래스가 매핑을 받게 쓰기, 또는 ORM |
| `CursorPagination` | 커서 조건을 QuerySet 의 `order_by()`·`filter()`로 만든다 | 키셋 페이지네이션을 직접: `.where(t.c.sku > last).order_by(t.c.sku).limit(n)` |

## 모델 인스턴스가 꼭 필요할 때

`ModelSerializer`의 관계 필드나 인스턴스 메서드를 부르는 템플릿처럼 모델 인스턴스가 꼭 필요하면, 지금 공개 API 로는 브리지 결과에서 인스턴스를 만드는 방법이 없습니다. ORM 으로 읽으세요. 브리지 쿼리로 PK 만 골라 `Product.objects.filter(pk__in=ids)`로 다시 읽는 방법은 쿼리가 하나 늘고 순서를 따로 맞춰야 하지만 지금 쓸 수 있습니다. 브리지 결과를 모델 인스턴스로 받는 기능은 v0.1 이후 후보([#17](https://github.com/itda-work/django-sqlalchemy-bridge/issues/17))에 있습니다.
