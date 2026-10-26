# 같은 요청, 두 구현: ORM 과 브리지

작은 상점([`tests/testapp/models.py`](../tests/testapp/models.py)의 `Category(name, parent)`·`Product(sku, category, price)`·`Sale(product, sold_at, quantity, amount)`·`DailyStock(product, day, quantity, counted_at)`·`PriceChange(sku, price)`·`SaleArchive`)에서 나올 법한 요청 여섯 개를 Django ORM 만으로, 그리고 브리지로 풀었습니다. ORM 쪽은 일부러 약하게 쓰지 않았습니다. Django 5.2·6.1 에서 되는 가장 나은 방법(`Window`로 거르기, 잘라 낸 `Prefetch`, `Subquery`, `bulk_create(update_conflicts=True)` 등)을 쓰고, 방법이 여럿이면 대표 둘을 두었습니다. ORM 이 비기거나 이기는 경우도 그대로 싣습니다.

**코드.** 아래 코드는 [`tests/comparisons.py`](../tests/comparisons.py)에 그대로 있고, 문서와 어긋나면 `tests/test_readme_examples.py`가 실패합니다. [`tests/test_comparisons.py`](../tests/test_comparisons.py)가 같은 데이터에서 각 구현이 요청의 첫 ORM 구현과 같은 결과를 내고 같은 행을 남기는지, 쿼리를 몇 개 보내는지(SAVEPOINT 제외)를 SQLite·PostgreSQL·MySQL·Oracle 에서 확인합니다. 그 DB 에 문법이 없는 구현만 건너뜁니다(`LATERAL`은 SQLite, `ON CONFLICT`·`RETURNING`은 MySQL·Oracle, 데이터를 바꾸는 CTE 는 PostgreSQL 밖). 공통 import 는 `from collections import defaultdict`, `from decimal import Decimal`, `import sqlalchemy as sa`, `from django.db import connection, transaction`, `from django.db.models import Exists, F, OuterRef, Prefetch, Subquery, Sum, Window`, `from django.db.models.functions import Coalesce, Rank, RowNumber`, `from sqlalchemy.dialects import postgresql, sqlite`, `from django_sqlalchemy_bridge import execute, table_for`이고, 브리지 코드는 아래 테이블을 씁니다.

<!-- example:cmp-tables -->
```python
categories = table_for(Category)
products = table_for(Product)
sales = table_for(Sale)
daily_stock = table_for(DailyStock)
price_changes = table_for(PriceChange)
archive = table_for(SaleArchive)
```
<!-- /example:cmp-tables -->

**수치.** 표는 [`bench/scenarios/compare.py`](../bench/scenarios/compare.py)가 위 함수를 그대로 불러 잰 결과 파일에서 `bench/readme.py`가 만듭니다(어긋나면 `tests/test_bench_readme.py`가 실패). 측정 방법은 README 의 "[성능과 메모리](../README.md#성능과-메모리)"와 같습니다: GitHub Actions `ubuntu-latest` 러너, 예열 1회 뒤 5회 중 최솟값, 매 회차 롤백하는 트랜잭션 안에서(ORM 의 `atomic()`은 그 안의 savepoint), tracemalloc 은 따로 한 번 더 돌린 피크입니다. 데이터는 규모마다 새로 넣습니다: 카테고리 50개에 상품을 고르게 나누고 상품마다 판매 10건(트리는 가지 4개씩, 잎 카테고리마다 상품 2개·판매 5건). 쿼리 수는 측정과 별도로 한 번 더 돌려 센 값입니다. 공유 VM 이라 10% 안팎의 차이는 순서가 바뀔 수 있습니다.

<!-- bench:env-postgres-compare -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres-compare -->

<!-- bench:env-sqlite-compare -->
SQLite 3.45.1 · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-sqlite-compare -->

## 1. 카테고리 하위 트리의 매출 합계

카테고리 하나 아래 모든 카테고리(깊이 제한 없음)의 판매 합계. 측정하는 트리는 루트 아래로 깊이 3·5·7, 가지 4개씩(카테고리 85·1,365·21,845개, 판매 640·10,240·163,840건)이고, 루트 바로 아래 첫 카테고리(트리의 4분의 1)를 묻습니다.

ORM 은 트리를 내려가는 방법이 둘입니다. 한 단계에 쿼리 하나씩(쿼리 수가 깊이를 따라 늘어남), 또는 카테고리 전체를 한 번에 읽어 파이썬에서 걷기(쿼리 2개, 대신 묻지 않은 카테고리까지 읽음).

<!-- example:cmp-tree-orm-levels -->
```python
def subtree_revenue_orm_levels(root_id):
    # 한 단계에 쿼리 하나씩 내려가며 하위 카테고리 id 를 모은다
    ids, level = [root_id], [root_id]
    while level:
        level = list(Category.objects.filter(parent_id__in=level).values_list("id", flat=True))
        ids += level
    total = Sale.objects.filter(product__category_id__in=ids).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")
```
<!-- /example:cmp-tree-orm-levels -->

<!-- example:cmp-tree-orm-python -->
```python
def subtree_revenue_orm_python(root_id):
    # 카테고리 전체의 (id, parent_id) 를 한 번에 읽어 파이썬에서 트리를 걷는다
    children = defaultdict(list)
    for category_id, parent_id in Category.objects.values_list("id", "parent_id"):
        children[parent_id].append(category_id)
    ids, stack = [], [root_id]
    while stack:
        ids.append(stack.pop())
        stack += children[ids[-1]]
    total = Sale.objects.filter(product__category_id__in=ids).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")
```
<!-- /example:cmp-tree-orm-python -->

브리지는 재귀 CTE 로 하위 트리와 합계를 쿼리 하나에 구합니다. 문장은 한 번 만들고 `bindparam`으로 재사용합니다.

<!-- example:cmp-tree-bridge -->
```python
tree = sa.select(categories.c.id).where(categories.c.id == sa.bindparam("root")).cte("tree", recursive=True)
tree = tree.union_all(sa.select(categories.c.id).join(tree, categories.c.parent_id == tree.c.id))
subtree_revenue = (
    sa.select(sa.func.coalesce(sa.func.sum(sales.c.amount), 0))
    .join(products, products.c.id == sales.c.product_id)
    .where(products.c.category_id.in_(sa.select(tree.c.id)))
)


def subtree_revenue_bridge(root_id):
    [(total,)] = execute(subtree_revenue, {"root": root_id})
    return total
```
<!-- /example:cmp-tree-bridge -->

<!-- bench:pg-compare-tree -->
| | 깊이 3 쿼리 | 깊이 3 시간 (ms) | 깊이 5 쿼리 | 깊이 5 시간 (ms) | 깊이 7 쿼리 | 깊이 7 시간 (ms) | 깊이 7 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|---:|---:|
| ORM: 한 단계에 쿼리 하나씩 | 4 | 3.24 | 6 | 9.56 | 8 | 90.1 | 2.3 |
| ORM: 카테고리 전체를 읽어 파이썬에서 | 2 | 1.87 | 2 | 5.58 | 2 | 62.1 | 3.7 |
| 브리지: 재귀 CTE | 1 | 0.98 | 1 | 2.36 | 1 | 25.6 | 0.0 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](../bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-tree -->

<!-- bench:sqlite-compare-tree -->
| | 깊이 3 쿼리 | 깊이 3 시간 (ms) | 깊이 5 쿼리 | 깊이 5 시간 (ms) | 깊이 7 쿼리 | 깊이 7 시간 (ms) | 깊이 7 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|---:|---:|
| ORM: 한 단계에 쿼리 하나씩 | 4 | 1.51 | 6 | 4.09 | 8 | 39.1 | 1.7 |
| ORM: 카테고리 전체를 읽어 파이썬에서 | 2 | 0.93 | 2 | 3.01 | 2 | 36.1 | 3.6 |
| 브리지: 재귀 CTE | 1 | 0.24 | 1 | 0.84 | 1 | 11.9 | 0.0 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-compare.json`](../bench/results/2026-10-26-sqlite-compare.json)</sub>
<!-- /bench:sqlite-compare-tree -->

**결론.** 브리지가 쿼리 하나로 가장 빠릅니다. PostgreSQL 에서 카테고리 전체를 파이썬으로 걷는 ORM 보다 1.9~2.4배(깊이 7: 62.1→25.6 ms), 한 단계씩 내려가는 ORM 보다 3.3~4.1배 빠르고, SQLite 에서는 각각 3.0~3.9배·3.3~6.3배입니다. 파이썬으로 걷는 ORM 은 묻지 않은 카테고리까지 읽어 깊이 7 에서 tracemalloc 피크가 3.7 MiB 입니다(브리지 0.0 MiB).

## 2. 상품마다 최근 판매 2건

상품마다 가장 최근 판매 두 건(같은 시각이면 id 가 큰 쪽).

ORM 은 Django 4.2 부터 윈도 함수로 거를 수 있고(`filter(n__lte=2)`를 바깥 쿼리로 감쌈), 잘라 낸 `Prefetch`도 같은 윈도 함수를 씁니다.

<!-- example:cmp-top2-orm-window -->
```python
def latest_sales_orm_window():
    # Django 4.2+ 는 윈도 함수로 거를 수 있다(바깥 쿼리로 감싼다)
    newest_first = Window(RowNumber(), partition_by=F("product_id"), order_by=[F("sold_at").desc(), F("id").desc()])
    rows = (
        Sale.objects.annotate(n=newest_first)
        .filter(n__lte=2)
        .order_by("product__sku", "-sold_at", "-id")
        .values_list("product__sku", "sold_at", "amount")
    )
    return list(rows)
```
<!-- /example:cmp-top2-orm-window -->

<!-- example:cmp-top2-orm-prefetch -->
```python
def latest_sales_orm_prefetch():
    # Django 4.2+ 는 잘라 낸 prefetch 를 상품별 윈도 함수로 한 쿼리에 읽는다
    two = Prefetch("sales", queryset=Sale.objects.order_by("-sold_at", "-id")[:2], to_attr="newest")
    return [
        (p.sku, s.sold_at, s.amount) for p in Product.objects.order_by("sku").prefetch_related(two) for s in p.newest
    ]
```
<!-- /example:cmp-top2-orm-prefetch -->

브리지는 `LATERAL`로 상품마다 판매 두 건을 고릅니다. SQLite 에는 `LATERAL`이 없어 SQLite 표에는 ORM 만 있습니다(브리지로 윈도 함수를 쓰면 ORM 의 첫 구현과 같은 SQL 입니다).

<!-- example:cmp-top2-bridge -->
```python
newest = (
    sa.select(sales.c.id, sales.c.sold_at, sales.c.amount)
    .where(sales.c.product_id == products.c.id)
    .order_by(sales.c.sold_at.desc(), sales.c.id.desc())
    .limit(2)
    .lateral("newest")
)
latest_sales = (
    sa.select(products.c.sku, newest.c.sold_at, newest.c.amount)
    .select_from(products)
    .join(newest, sa.true())
    .order_by(products.c.sku, newest.c.sold_at.desc(), newest.c.id.desc())
)


def latest_sales_bridge():  # LATERAL: PostgreSQL, MySQL, Oracle (SQLite 에는 없다)
    return execute(latest_sales)
```
<!-- /example:cmp-top2-bridge -->

<!-- bench:pg-compare-latest -->
| | 상품 1,000 쿼리 | 상품 1,000 시간 (ms) | 상품 10,000 쿼리 | 상품 10,000 시간 (ms) | 상품 10,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: `Window(RowNumber())`로 거르기 | 1 | 13.1 | 1 | 119 | 5.5 |
| ORM: 잘라 낸 `Prefetch` | 2 | 36.2 | 2 | 433 | 24.1 |
| 브리지: `LATERAL` | 1 | 9.46 | 1 | 86.6 | 5.4 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](../bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-latest -->

<!-- bench:sqlite-compare-latest -->
| | 상품 1,000 쿼리 | 상품 1,000 시간 (ms) | 상품 10,000 쿼리 | 상품 10,000 시간 (ms) | 상품 10,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: `Window(RowNumber())`로 거르기 | 1 | 21.4 | 1 | 203 | 7.8 |
| ORM: 잘라 낸 `Prefetch` | 2 | 45.5 | 2 | 555 | 24.7 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-compare.json`](../bench/results/2026-10-26-sqlite-compare.json)</sub>
<!-- /bench:sqlite-compare-latest -->

**결론.** ORM 이 거의 비기는 경우입니다. PostgreSQL 에서 브리지 `LATERAL`이 ORM 윈도 함수보다 1.4배 빠르지만(상품 10,000개: 118.8→86.6 ms) 둘 다 쿼리 하나이고 메모리도 같습니다(5.5 대 5.4 MiB). SQLite 에는 `LATERAL`이 없으니 ORM 윈도 함수를 쓰면 됩니다. 잘라 낸 `Prefetch`는 모델 인스턴스를 만들어 ORM 윈도 함수보다 PostgreSQL 에서 2.8~3.6배, SQLite 에서 2.1~2.7배 느립니다.

## 3. 일별 재고 업서트 후 결과 반환

(상품, 날짜)마다 센 수량을 더하고(행이 없으면 새로 넣고) 결과 행을 돌려받기. 측정은 한 날짜의 입력 중 절반이 이미 행이 있는 경우입니다.

ORM 의 `bulk_create(update_conflicts=True)`는 충돌 행을 입력 값으로 덮어쓸 뿐 `quantity + n`을 쓸 수 없습니다. 그래서 행마다 `update(quantity=F("quantity") + n)`을 하고 맞는 행이 없으면 `create()`하거나, 있는 행을 잠가 읽어 파이썬에서 더한 뒤 `bulk_create(update_conflicts=True)` 한 번으로 씁니다. 두 번째는 MySQL(`unique_fields`를 받지 않음)과 Oracle(`update_conflicts` 없음)에서 돌지 않습니다.

<!-- example:cmp-upsert-orm-loop -->
```python
def add_stock_orm_loop(counts):
    # 행마다 UPDATE(quantity + n), 맞는 행이 없으면 INSERT. 끝에 결과를 다시 읽는다
    with transaction.atomic():
        for c in counts:
            found = DailyStock.objects.filter(product_id=c["product_id"], day=c["day"]).update(
                quantity=F("quantity") + c["quantity"], counted_at=c["counted_at"]
            )
            if not found:
                DailyStock.objects.create(**c)
        keys = {(c["product_id"], c["day"]) for c in counts}
        rows = DailyStock.objects.filter(product_id__in={p for p, _ in keys}, day__in={d for _, d in keys}).values_list(
            "product_id", "day", "quantity", "counted_at"
        )
        return sorted(row for row in rows if row[:2] in keys)
```
<!-- /example:cmp-upsert-orm-loop -->

<!-- example:cmp-upsert-orm-bulk -->
```python
def add_stock_orm_bulk(counts):
    # 있는 행을 잠가 읽고 파이썬에서 더한 뒤 INSERT ... ON CONFLICT DO UPDATE 한 번으로 쓴다
    with transaction.atomic():
        have = DailyStock.objects.select_for_update().filter(
            product_id__in={c["product_id"] for c in counts}, day__in={c["day"] for c in counts}
        )
        quantity = {(s.product_id, s.day): s.quantity for s in have}
        rows = [
            DailyStock(**{**c, "quantity": quantity.get((c["product_id"], c["day"]), 0) + c["quantity"]})
            for c in counts
        ]
        DailyStock.objects.bulk_create(
            rows, update_conflicts=True, unique_fields=["product", "day"], update_fields=["quantity", "counted_at"]
        )
    return sorted((r.product_id, r.day, r.quantity, r.counted_at) for r in rows)
```
<!-- /example:cmp-upsert-orm-bulk -->

브리지는 여러 행 `VALUES`의 `INSERT … ON CONFLICT DO UPDATE … RETURNING` 한 문장입니다(PostgreSQL·SQLite). 입력마다 문장이 달라 매번 컴파일합니다.

<!-- example:cmp-upsert-bridge -->
```python
def add_stock_bridge(counts):  # ON CONFLICT + RETURNING: PostgreSQL, SQLite
    insert = postgresql.insert if connection.vendor == "postgresql" else sqlite.insert
    stmt = insert(daily_stock).values(counts)
    stmt = stmt.on_conflict_do_update(
        index_elements=[daily_stock.c.product_id, daily_stock.c.day],
        set_={"quantity": daily_stock.c.quantity + stmt.excluded.quantity, "counted_at": stmt.excluded.counted_at},
    ).returning(daily_stock.c.product_id, daily_stock.c.day, daily_stock.c.quantity, daily_stock.c.counted_at)
    return sorted(execute(stmt))
```
<!-- /example:cmp-upsert-bridge -->

<!-- bench:pg-compare-upsert -->
| | 100행 쿼리 | 100행 시간 (ms) | 1,000행 쿼리 | 1,000행 시간 (ms) | 5,000행 쿼리 | 5,000행 시간 (ms) | 5,000행 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|---:|---:|
| ORM: 행마다 `update()`, 없으면 `create()` | 151 | 81.0 | 1,501 | 801 | 7,501 | 4,241 | 3.5 |
| ORM: 잠가 읽기 + `bulk_create(update_conflicts=True)` | 2 | 5.87 | 2 | 38.2 | 2 | 179 | 6.9 |
| 브리지: `ON CONFLICT DO UPDATE … RETURNING` | 1 | 8.48 | 1 | 72.9 | 1 | 407 | 16.2 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](../bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-upsert -->

<!-- bench:sqlite-compare-upsert -->
| | 100행 쿼리 | 100행 시간 (ms) | 1,000행 쿼리 | 1,000행 시간 (ms) | 5,000행 쿼리 | 5,000행 시간 (ms) | 5,000행 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|---:|---:|
| ORM: 행마다 `update()`, 없으면 `create()` | 151 | 37.8 | 1,501 | 373 | 7,501 | 1,879 | 3.1 |
| ORM: 잠가 읽기 + `bulk_create(update_conflicts=True)` | 2 | 3.78 | 2 | 28.6 | 2 | 182 | 9.5 |
| 브리지: `ON CONFLICT DO UPDATE … RETURNING` | 1 | 6.39 | 1 | 55.7 | 1 | 286 | 11.6 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-compare.json`](../bench/results/2026-10-26-sqlite-compare.json)</sub>
<!-- /bench:sqlite-compare-upsert -->

**결론.** ORM 이 나은 경우입니다. 잠가 읽은 뒤 `bulk_create(update_conflicts=True)`하는 ORM 이 브리지보다 PostgreSQL 에서 1.4~2.3배, SQLite 에서 1.6~2.0배 빠릅니다. 브리지는 쿼리 하나를 아끼지만 호출마다 행 수만큼 커지는 `VALUES` 문장을 새로 컴파일하고, 5,000행에서 tracemalloc 피크도 더 큽니다(PostgreSQL 16.2 대 6.9 MiB). 브리지가 나은 점은 갱신이 한 문장이라 잠가 읽을 필요가 없다는 것입니다. 행마다 `update()`·`create()`하는 흔한 방법은 쿼리가 행 수의 약 1.5배이고, 브리지보다 PostgreSQL 에서 9.5~11배, SQLite 에서 5.9~6.7배 느립니다.

## 4. 가격표로 바뀐 상품만 갱신하고 바뀐 행 반환

따로 올려 둔 가격표(`PriceChange`)에서 가격이 다른 상품만 고치고, 고친 상품과 새 가격을 돌려받기. 측정은 모든 상품이 가격표에 있고 절반의 가격이 바뀐 경우입니다.

ORM 의 `update()`는 행을 돌려주지 않으므로 바뀔 행을 먼저 잠가 읽습니다. 그다음 상관 서브쿼리 UPDATE 한 번, 또는 모델로 읽어 `bulk_update()`(Django 기본 배치가 PostgreSQL 에서 크게 느려지므로 `batch_size=1000`, README "성능과 메모리" 1).

<!-- example:cmp-prices-orm-subquery -->
```python
def apply_prices_orm_subquery():
    # 바뀔 행을 먼저 읽고(UPDATE 는 행을 돌려주지 않는다) 상관 서브쿼리 UPDATE 한 번
    new_price = PriceChange.objects.filter(sku=OuterRef("sku")).values("price")
    differs = PriceChange.objects.filter(sku=OuterRef("sku")).exclude(price=OuterRef("price"))
    with transaction.atomic():
        targets = Product.objects.filter(Exists(differs))
        changed = list(targets.select_for_update().annotate(new=Subquery(new_price)).values_list("sku", "new"))
        targets.update(price=Subquery(new_price))
    return sorted(changed)
```
<!-- /example:cmp-prices-orm-subquery -->

<!-- example:cmp-prices-orm-bulk-update -->
```python
def apply_prices_orm_bulk_update():
    # 바뀔 상품을 새 가격과 함께 모델로 읽어 bulk_update()
    new_price = PriceChange.objects.filter(sku=OuterRef("sku")).values("price")
    differs = PriceChange.objects.filter(sku=OuterRef("sku")).exclude(price=OuterRef("price"))
    with transaction.atomic():
        changed = list(Product.objects.select_for_update().filter(Exists(differs)).annotate(new=Subquery(new_price)))
        for product in changed:
            product.price = product.new
        Product.objects.bulk_update(changed, ["price"], batch_size=1000)
    return sorted((p.sku, p.price) for p in changed)
```
<!-- /example:cmp-prices-orm-bulk-update -->

브리지는 `UPDATE … FROM … RETURNING` 한 문장입니다(PostgreSQL·SQLite. MySQL 은 `RETURNING` 없이 같은 문장이 `UPDATE products, changes …`로 컴파일됩니다).

<!-- example:cmp-prices-bridge -->
```python
apply_prices = (
    sa.update(products)
    .where(products.c.sku == price_changes.c.sku, products.c.price != price_changes.c.price)
    .values(price=price_changes.c.price)
    .returning(products.c.sku, products.c.price)
)


def apply_prices_bridge():  # UPDATE ... FROM ... RETURNING: PostgreSQL, SQLite
    return sorted(execute(apply_prices))
```
<!-- /example:cmp-prices-bridge -->

<!-- bench:pg-compare-prices -->
| | 상품 1,000 쿼리 | 상품 1,000 시간 (ms) | 상품 10,000 쿼리 | 상품 10,000 시간 (ms) | 상품 10,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 읽기 + `Subquery` UPDATE | 2 | 10.5 | 2 | 82.5 | 1.4 |
| ORM: 읽기 + `bulk_update(batch_size=1000)` | 2 | 66.9 | 6 | 765 | 11.0 |
| 브리지: `UPDATE … FROM … RETURNING` | 1 | 5.82 | 1 | 54.5 | 1.1 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](../bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-prices -->

<!-- bench:sqlite-compare-prices -->
| | 상품 1,000 쿼리 | 상품 1,000 시간 (ms) | 상품 10,000 쿼리 | 상품 10,000 시간 (ms) | 상품 10,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 읽기 + `Subquery` UPDATE | 2 | 4.58 | 2 | 31.2 | 1.5 |
| ORM: 읽기 + `bulk_update(batch_size=1000)` | 2 | 58.0 | 6 | 664 | 11.1 |
| 브리지: `UPDATE … FROM … RETURNING` | 1 | 2.13 | 1 | 19.5 | 1.5 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-compare.json`](../bench/results/2026-10-26-sqlite-compare.json)</sub>
<!-- /bench:sqlite-compare-prices -->

**결론.** 브리지가 서브쿼리 ORM 보다 PostgreSQL 에서 1.5~1.8배(상품 10,000개: 82.5→54.5 ms), SQLite 에서 1.6~2.2배 빠릅니다. 모델로 읽어 `bulk_update()`하는 ORM 은 `batch_size=1000`이어도 PostgreSQL 에서 브리지보다 12~14배 느리고, 5,000개를 바꾸는 데 쿼리 6개(읽기 1 + 배치 5)를 보냅니다.

## 5. 오래된 판매를 보관 테이블로 옮기기

기준 시각 전의 판매를 id 를 유지한 채 `SaleArchive`로 옮기고 `Sale`에서 지우기. 측정은 판매의 절반쯤을 옮깁니다.

ORM 에는 `INSERT … SELECT`가 없어 행을 파이썬으로 읽어 모델로 만든 뒤 `bulk_create()`하고 지웁니다.

<!-- example:cmp-archive-orm -->
```python
def archive_sales_orm(cutoff):
    # 읽어서 모델로 만들고 bulk_create, 그리고 DELETE
    with transaction.atomic():
        old = list(Sale.objects.select_for_update().filter(sold_at__lt=cutoff))
        SaleArchive.objects.bulk_create(
            SaleArchive(id=s.id, product_id=s.product_id, sold_at=s.sold_at, quantity=s.quantity, amount=s.amount)
            for s in old
        )
        Sale.objects.filter(sold_at__lt=cutoff).delete()
    return len(old)
```
<!-- /example:cmp-archive-orm -->

브리지는 `INSERT … SELECT`와 `DELETE` 두 문장(모든 백엔드), PostgreSQL 에서는 데이터를 바꾸는 CTE 한 문장도 됩니다. 두 문장 방식은 ORM 방식처럼 두 문장 사이에 들어온 행을 지울 수 있지만, CTE 는 한 스냅숏에서 지운 행만 넣습니다.

<!-- example:cmp-archive-bridge -->
```python
columns = ["id", "product_id", "sold_at", "quantity", "amount"]
is_old = sales.c.sold_at < sa.bindparam("cutoff")
copy_old = sa.insert(archive).from_select(columns, sa.select(*[sales.c[name] for name in columns]).where(is_old))
delete_old = sa.delete(sales).where(is_old)


def archive_sales_bridge(cutoff):  # INSERT ... SELECT 와 DELETE: 모든 백엔드
    with transaction.atomic():
        moved = execute(copy_old, {"cutoff": cutoff})
        execute(delete_old, {"cutoff": cutoff})
    return moved
```
<!-- /example:cmp-archive-bridge -->

<!-- example:cmp-archive-bridge-cte -->
```python
moved = sa.delete(sales).where(is_old).returning(*[sales.c[name] for name in columns]).cte("moved")
move_old = sa.insert(archive).from_select(columns, sa.select(moved))


def archive_sales_bridge_cte(cutoff):  # 데이터를 바꾸는 CTE, 문장 하나: PostgreSQL
    return execute(move_old, {"cutoff": cutoff})
```
<!-- /example:cmp-archive-bridge-cte -->

<!-- bench:pg-compare-archive -->
| | 판매 10,000 쿼리 | 판매 10,000 시간 (ms) | 판매 100,000 쿼리 | 판매 100,000 시간 (ms) | 판매 100,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 읽기 + `bulk_create()` + `delete()` | 3 | 153 | 3 | 1,799 | 79.1 |
| 브리지: `INSERT … SELECT` + `DELETE` | 2 | 15.7 | 2 | 157 | 0.0 |
| 브리지: 데이터를 바꾸는 CTE | 1 | 17.1 | 1 | 173 | 0.0 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](../bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-archive -->

<!-- bench:sqlite-compare-archive -->
| | 판매 10,000 쿼리 | 판매 10,000 시간 (ms) | 판매 100,000 쿼리 | 판매 100,000 시간 (ms) | 판매 100,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 읽기 + `bulk_create()` + `delete()` | 3 | 195 | 4 | 2,456 | 110.1 |
| 브리지: `INSERT … SELECT` + `DELETE` | 2 | 8.51 | 2 | 93.2 | 0.0 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-compare.json`](../bench/results/2026-10-26-sqlite-compare.json)</sub>
<!-- /bench:sqlite-compare-archive -->

**결론.** 브리지가 PostgreSQL 에서 10~11배, SQLite 에서 23~26배 빠르고, 행이 파이썬을 거치지 않아 클라이언트 메모리를 거의 쓰지 않습니다(판매 100,000건에서 ORM 의 tracemalloc 피크는 PostgreSQL 79.1 MiB, SQLite 110.1 MiB). 데이터를 바꾸는 CTE 는 두 문장보다 9~10% 느렸지만 한 스냅숏에서 옮깁니다. SQLite 에서 ORM 의 `bulk_create()`는 판매 100,000건에서 배치로 나뉘어 쿼리가 4개입니다.

## 6. 카테고리별 매출 순위와 누적 합계

상품마다 매출, 카테고리 안 순위(`RANK()`), 순위 순 누적 합계.

ORM 은 `Sum()` 집계 위에 `Window(Sum())`을 얹지 못합니다(`FieldError: Cannot compute Sum('revenue'): 'revenue' is an aggregate`). 그래서 매출을 상품별 상관 서브쿼리로 만들고 그 위에 윈도 함수를 씁니다. 순위만이면 `Window(Rank(), order_by=Sum(...).desc())`로 집계 위에 쓸 수 있습니다(README "성능과 메모리" 6).

<!-- example:cmp-report-orm -->
```python
def category_report_orm():
    # Sum() 집계 위에는 Window(Sum()) 을 못 얹어서(FieldError) 매출을 상관 서브쿼리로 만든다
    per_product = Sale.objects.filter(product=OuterRef("pk")).values("product").annotate(s=Sum("amount")).values("s")
    in_category = {"partition_by": F("category_id")}
    rows = (
        Product.objects.annotate(revenue=Coalesce(Subquery(per_product), Decimal("0")))
        .annotate(
            rank=Window(Rank(), order_by=F("revenue").desc(), **in_category),
            running=Window(Sum("revenue"), order_by=[F("revenue").desc(), F("sku")], **in_category),
        )
        .order_by("category__name", "rank", "sku")
        .values_list("category__name", "sku", "revenue", "rank", "running")
    )
    return list(rows)
```
<!-- /example:cmp-report-orm -->

브리지는 집계를 CTE 로 두고 그 위에 윈도 함수 두 개를 씁니다.

<!-- example:cmp-report-bridge -->
```python
revenue = (
    sa.select(
        products.c.sku,
        products.c.category_id,
        sa.func.coalesce(sa.func.sum(sales.c.amount), 0).label("revenue"),
    )
    .outerjoin(sales, sales.c.product_id == products.c.id)
    .group_by(products.c.id, products.c.sku, products.c.category_id)
    .cte("revenue")
)
in_category = {"partition_by": revenue.c.category_id}
rank = sa.func.rank().over(order_by=revenue.c.revenue.desc(), **in_category).label("rank")
running = sa.func.sum(revenue.c.revenue).over(order_by=(revenue.c.revenue.desc(), revenue.c.sku), **in_category)
category_report = (
    sa.select(categories.c.name, revenue.c.sku, revenue.c.revenue, rank, running.label("running"))
    .join(categories, categories.c.id == revenue.c.category_id)
    .order_by(categories.c.name, rank, revenue.c.sku)
)


def category_report_bridge():
    return execute(category_report)
```
<!-- /example:cmp-report-bridge -->

<!-- bench:pg-compare-report -->
| | 상품 1,000 쿼리 | 상품 1,000 시간 (ms) | 상품 10,000 쿼리 | 상품 10,000 시간 (ms) | 상품 10,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 상관 `Subquery` + `Window` | 1 | 12.9 | 1 | 124 | 4.7 |
| 브리지: CTE + 윈도 함수 | 1 | 7.49 | 1 | 75.6 | 3.9 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](../bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-report -->

<!-- bench:sqlite-compare-report -->
| | 상품 1,000 쿼리 | 상품 1,000 시간 (ms) | 상품 10,000 쿼리 | 상품 10,000 시간 (ms) | 상품 10,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 상관 `Subquery` + `Window` | 1 | 23.9 | 1 | 233 | 5.2 |
| 브리지: CTE + 윈도 함수 | 1 | 9.83 | 1 | 96.5 | 5.1 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-compare.json`](../bench/results/2026-10-26-sqlite-compare.json)</sub>
<!-- /bench:sqlite-compare-report -->

**결론.** 둘 다 쿼리 하나지만 브리지가 PostgreSQL 에서 1.6~1.7배(상품 10,000개: 124.1→75.6 ms), SQLite 에서 2.4배 빠릅니다. ORM 은 집계 대신 상품마다 상관 서브쿼리로 매출을 구해야 했습니다. 순위만 필요하면 ORM 도 집계 위에 `Window(Rank())`를 쓸 수 있고, 그 경우 도구 차이는 거의 없습니다(README "성능과 메모리" 6).
