# django-sqlalchemy-bridge

> ⚠️ **실험적 프로젝트입니다 (Experimental).** API·동작·지원 범위가 예고 없이 바뀔 수 있고, 운영 환경에서 쓰는 것을 권하지 않습니다.
>
> **상태: 0.2 (알파).** 공개 API 는 모델 → `sa.Table`(`table_for`), `execute`, `execute_result`, `stream`, `bulk_insert`/`bulk_update`, `execute_pipeline`, `raw`, `copy_to`, `arun`, `compare_table`/`assert_table_matches`이고, SQLite·PostgreSQL·MySQL 8.4·Oracle 23ai 에서 테스트합니다. 0.x 동안은 마이너 버전 사이에도 API가 바뀔 수 있으니 버전을 고정해 쓰세요.

Django가 연 **연결과 트랜잭션 안에서** SQLAlchemy 2.x Core 표현식(대량 DML, CTE, 윈도 함수, 복잡한 조인)을 쓰게 해 주는 라이브러리입니다.

## 왜 쓰나

**표현력 때문입니다.** Django ORM 으로 쓰기 어려운 SQL(재귀 CTE, 여러 CTE, `INSERT … ON CONFLICT … RETURNING`, `UPDATE … FROM`, `INSERT … SELECT`, PostgreSQL 의 `LATERAL`·`DISTINCT ON`·데이터를 바꾸는 CTE)을 raw SQL 문자열 대신 SQLAlchemy 식으로 씁니다. 그러면서도 연결·트랜잭션·값 변환은 Django 의 것을 그대로 씁니다. `atomic()` 안에서 ORM 쿼리와 섞여 돌고, 결과의 datetime·Decimal·JSON 은 ORM 이 읽은 값과 같습니다. raw SQL 과 달리 테이블·컬럼 이름이 모델에서 나오므로 오타가 실행 전에 드러나고, 백엔드마다 SQL 을 따로 쓰지 않아도 되는 경우가 많습니다.

성능은 목적이 아닙니다. 대량 쓰기에서는 따라옵니다. `bulk_update`는 Django `bulk_update`보다 한 자릿수 이상, `bulk_insert`는 Django `bulk_create`보다 빠릅니다(PostgreSQL 에서 25% 안팎, `method="copy"`면 2배 이상). 같은 SQL 이면 실행 시간이 DB 에서 정해져 ORM 과 거의 같고(아래 "성능과 메모리"), ORM 으로는 쿼리를 더 보내거나 행을 파이썬으로 옮겨야 하는 요청에서 빨라집니다. 반대로 ORM 이 더 빠른 요청도 있습니다(아래 "[같은 요청, 두 구현](#같은-요청-두-구현)").

**언제 ORM 을 쓰나.** ORM 으로 자연스럽게 써지는 쿼리, 한 행씩 읽고 쓰는 CRUD, signal·`save()`·필드 `default`·검증이 따라와야 하는 쓰기는 ORM 을 쓰세요. 브리지의 쓰기는 모델 계층을 거치지 않습니다(아래 "대량 쓰기"의 경고). 자세한 경우는 "[브리지가 도움이 안 되는 경우](#브리지가-도움이-안-되는-경우)"에 있습니다.

### 예제

아래 코드는 테스트(`tests/test_examples.py`)에서 그대로 돌고, README 가 그 코드와 어긋나면 테스트가 실패합니다. 모델은 작은 상점([`tests/testapp/models.py`](tests/testapp/models.py)의 `Category`·`Product`·`Sale`·`DailyStock`·`PriceChange`)이고, 테스트 파일에는 LATERAL·DISTINCT ON·여러 CTE·윈도 함수·`INSERT … SELECT`·`ON CONFLICT DO NOTHING`·데이터를 바꾸는 CTE 예제가 더 있습니다. 모든 예제는 SQLite·PostgreSQL·MySQL·Oracle 에서 돌고, 그 DB 에 없는 문법(PostgreSQL 전용 `DISTINCT ON` 등, MySQL·Oracle 에 없는 `ON CONFLICT`·`RETURNING`, Oracle 에 없는 `UPDATE … FROM`)만 건너뜁니다. 예제 코드의 import 는 `import datetime`, `from zoneinfo import ZoneInfo`, `import sqlalchemy as sa`, `from django.db import connection, transaction`, `from sqlalchemy.dialects import mysql, postgresql, sqlite`, `from django_sqlalchemy_bridge import execute, table_for`입니다.

재귀 CTE: 카테고리 하나의 하위 트리를 깊이와 함께.

<!-- example:recursive-cte -->
```python
categories = table_for(Category)

tree = (
    sa.select(categories.c.id, categories.c.name, sa.literal_column("0").label("depth"))
    .where(categories.c.id == sa.bindparam("root"))
    .cte("tree", recursive=True)
)
tree = tree.union_all(
    sa.select(categories.c.id, categories.c.name, tree.c.depth + 1).join(tree, categories.c.parent_id == tree.c.id)
)
subtree = sa.select(tree.c.name, tree.c.depth).order_by(tree.c.depth, tree.c.name)

rows = execute(subtree, {"root": clothes.pk})
# [("의류", 0), ("상의", 1), ("하의", 1)]
```
<!-- /example:recursive-cte -->

업서트: 같은 (상품, 날짜) 행이 있으면 수량을 더하고, 결과 행을 돌려받습니다. `ON CONFLICT`는 백엔드마다 SQLAlchemy 구성자가 달라 vendor 로 고릅니다. `RETURNING`의 날짜·시각도 ORM 처럼 변환됩니다.

<!-- example:upsert -->
```python
daily_stock = table_for(DailyStock)
insert = postgresql.insert if connection.vendor == "postgresql" else sqlite.insert

stmt = insert(daily_stock)  # 컬럼은 아래 params 가 정한다
stmt = stmt.on_conflict_do_update(
    index_elements=[daily_stock.c.product_id, daily_stock.c.day],
    set_={"quantity": daily_stock.c.quantity + stmt.excluded.quantity, "counted_at": stmt.excluded.counted_at},
).returning(daily_stock.c.day, daily_stock.c.quantity, daily_stock.c.counted_at)

counted_at = datetime.datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("Asia/Seoul"))
params = {"product_id": shirt.pk, "day": counted_at.date(), "quantity": 3, "counted_at": counted_at}
with transaction.atomic():
    rows = execute(stmt, params)
# [(datetime.date(2026, 10, 26), 10, datetime.datetime(2026, 10, 26, 9, 0, tzinfo=datetime.timezone.utc))]
```
<!-- /example:upsert -->

MySQL 에는 `ON CONFLICT`도 `RETURNING`도 없어 같은 업서트를 `ON DUPLICATE KEY UPDATE`로 쓰고 rowcount 를 받습니다(넣은 행은 1, 갱신한 행은 2 로 셉니다).

<!-- example:upsert-mysql -->
```python
daily_stock = table_for(DailyStock)

stmt = mysql.insert(daily_stock)  # 컬럼은 아래 params 가 정한다
stmt = stmt.on_duplicate_key_update(
    quantity=daily_stock.c.quantity + stmt.inserted.quantity, counted_at=stmt.inserted.counted_at
)

counted_at = datetime.datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("Asia/Seoul"))
params = {"product_id": shirt.pk, "day": counted_at.date(), "quantity": 3, "counted_at": counted_at}
with transaction.atomic():
    count = execute(stmt, params)  # 2: MySQL 은 갱신한 행을 2, 넣은 행을 1 로 센다
```
<!-- /example:upsert-mysql -->

`UPDATE … FROM`: 따로 올려 둔 가격표를 바뀐 행에만 적용하고 바뀐 행을 돌려받습니다(SQLite 3.33+, PostgreSQL). MySQL 에서는 `.returning()` 없이 같은 문장이 `UPDATE products, changes SET …`로 컴파일되어 rowcount 를 돌려줍니다.

<!-- example:update-from -->
```python
products = table_for(Product)
changes = table_for(PriceChange)

stmt = (
    sa.update(products)
    .where(products.c.sku == changes.c.sku, products.c.price != changes.c.price)
    .values(price=changes.c.price)
    .returning(products.c.sku, products.c.price)
)
changed = execute(stmt)  # [("SHIRT", Decimal("27.50"))]
```
<!-- /example:update-from -->

**aldjemy 와의 관계.** [aldjemy](https://github.com/aldjemy/aldjemy)가 같은 문제를 먼저 풀었지만, SQLAlchemy 내부 비공개 풀 구조에 기대 Django 연결을 공유하는 탓에 SQLAlchemy 2 미만에 묶여 있고, Django 6.0부터는 모델 변환 단계에서도 깨집니다. 이 프로젝트는 SQLAlchemy 에 연결을 넘기지 않고 Django 커서로 실행합니다(아래 "설계 원칙").

## 같은 요청, 두 구현

실제로 나올 법한 요청 하나를 Django ORM 만으로 푼 코드와 브리지로 푼 코드를 나란히 놓고 쿼리 수와 시간을 쟀습니다. ORM 쪽은 Django 5.2·6.1 에서 되는 가장 나은 방법으로 썼습니다. 여섯 요청 가운데 넷을 싣고, 여섯 개 전부와 ORM 의 다른 구현, SQLite 수치, 메모리는 [`docs/comparisons.md`](docs/comparisons.md)에 있습니다. 코드는 [`tests/comparisons.py`](tests/comparisons.py)에서 그대로 옮겼고(두 구현의 결과·남긴 행·쿼리 수를 `tests/test_comparisons.py`가 SQLite·PostgreSQL·MySQL·Oracle 에서 확인), 표는 아래 환경의 GitHub Actions 러너에서 잰 결과 파일로 만듭니다(측정 방법은 "[성능과 메모리](#성능과-메모리)"와 같고, 쿼리 수는 SAVEPOINT 를 뺀 값). 브리지 코드의 `categories`·`products`·`sales`·`daily_stock`·`price_changes`·`archive`는 모델마다 `table_for(Model)`입니다.

<!-- bench:env-postgres-compare -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres-compare -->

### 카테고리 하위 트리의 매출 합계

카테고리 하나 아래 모든 카테고리의 판매 합계. ORM 은 트리를 걸을 재귀 쿼리가 없어 카테고리를 한 단계씩 내려가거나(깊이만큼 쿼리), 아래처럼 카테고리 전체를 읽어 파이썬에서 걷습니다. 브리지는 재귀 CTE 쿼리 하나입니다.

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

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-tree -->

브리지가 깊이 5·7 에서 파이썬으로 걷는 ORM 보다 2.4배 빠르고(5.58→2.36 ms, 62.1→25.6 ms), 한 단계씩 내려가는 ORM 보다 3.5배 이상 빠릅니다.

### 가격표로 바뀐 상품만 갱신하고 바뀐 행 반환

ORM 의 `update()`는 행을 돌려주지 않아 바뀔 행을 먼저 잠가 읽고 상관 서브쿼리로 UPDATE 합니다. 브리지는 `UPDATE … FROM … RETURNING` 한 문장입니다(PostgreSQL·SQLite).

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

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-prices -->

브리지가 서브쿼리 ORM 보다 1.5~1.8배 빠릅니다(상품 10,000개 중 5,000개 변경에서 82.5→54.5 ms). 모델로 읽어 `bulk_update()`하는 흔한 방법보다는 14배 빠릅니다(765 ms).

### 오래된 판매를 보관 테이블로 옮기기

ORM 에는 `INSERT … SELECT`가 없어 행을 파이썬으로 읽어 모델을 만들고 `bulk_create()`한 뒤 지웁니다. 브리지는 `INSERT … SELECT`와 `DELETE` 두 문장이라 행이 파이썬을 거치지 않습니다(PostgreSQL 에서는 데이터를 바꾸는 CTE 한 문장도 됩니다).

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

<!-- bench:pg-compare-archive -->
| | 판매 10,000 쿼리 | 판매 10,000 시간 (ms) | 판매 100,000 쿼리 | 판매 100,000 시간 (ms) | 판매 100,000 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|---:|
| ORM: 읽기 + `bulk_create()` + `delete()` | 3 | 153 | 3 | 1,799 | 79.1 |
| 브리지: `INSERT … SELECT` + `DELETE` | 2 | 15.7 | 2 | 157 | 0.0 |
| 브리지: 데이터를 바꾸는 CTE | 1 | 17.1 | 1 | 173 | 0.0 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-archive -->

브리지가 10~11배 빠르고(판매 100,000건 중 약 절반을 옮기는 데 1,799→157 ms), 클라이언트 메모리를 거의 쓰지 않습니다(tracemalloc 피크 79.1 MiB 대 0.0 MiB).

### 일별 재고 업서트 후 결과 반환: ORM 이 나은 경우

(상품, 날짜)마다 센 수량을 더하고 없으면 넣은 뒤 결과 행을 돌려받습니다. ORM `bulk_create(update_conflicts=True)`는 기존 값에 더하지 못해 있는 행을 잠가 읽고 파이썬에서 더한 뒤 한 번에 씁니다. 브리지는 여러 행 `VALUES`의 `INSERT … ON CONFLICT DO UPDATE … RETURNING` 한 문장입니다.

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

<sub>결과 파일: [`bench/results/2026-10-26-postgres-compare.json`](bench/results/2026-10-26-postgres-compare.json)</sub>
<!-- /bench:pg-compare-upsert -->

쿼리는 브리지가 하나 적지만 ORM `bulk_create()`가 1.4~2.3배 빠릅니다(5,000행에서 179 대 407 ms). 입력마다 문장이 달라 브리지는 호출마다 행 수 × 컬럼 수만큼 bind 가 있는 `VALUES` 문장을 SQLAlchemy 로 새로 컴파일하기 때문입니다(로컬 프로파일에서 가장 큰 비용이 컴파일이었습니다). 브리지의 이점은 갱신이 한 문장이라 읽기와 쓰기 사이에 잠금이 필요 없다는 것과, 행마다 `update()`·`create()`하는 흔한 방법(5,000행에 쿼리 7,501개, 4,241 ms)보다 10배 빠르다는 데 있습니다.

## AI 코딩 에이전트에게

이 라이브러리는 새로 나와 AI 모델의 학습 데이터에 거의 없습니다. 그래서 설치된 패키지 안에 에이전트용 요약 [`django_sqlalchemy_bridge/llms.txt`](django_sqlalchemy_bridge/llms.txt)(저장소 루트 [`llms.txt`](llms.txt)와 같은 내용)를 넣어 두었습니다. 언제 쓰고 언제 ORM 을 쓰는지, 꼭 지킬 것(SQLAlchemy `Engine`·`Session`을 만들지 않기, 쓰기가 signal·`save()`를 우회한다는 것, `bindparam` 재사용), 지원하지 않는 것, 공개 API 시그니처, 위 예제의 요약이 들어 있습니다. 전 세계 사용자의 에이전트가 읽는 파일이라 영어로 씁니다. 내 프로젝트의 `AGENTS.md`나 `CLAUDE.md`에 아래를 붙여 두면 에이전트가 설치된 버전의 안내를 읽습니다.

```markdown
## django-sqlalchemy-bridge
This project runs SQLAlchemy Core statements on Django's connections and transactions with django-sqlalchemy-bridge.
Before writing code with it, read and follow the guide of the installed version (prefix `uv run` in a uv project):
`python -c "import importlib.resources as r; print(r.files('django_sqlalchemy_bridge').joinpath('llms.txt').read_text('utf-8'))"`
Do not create a SQLAlchemy `Engine` or `Session`. Writes through the bridge skip signals and `save()`.
```

## 설계 원칙

- **연결과 트랜잭션은 Django가 관리합니다.** SQLAlchemy는 SQL을 만들고 컴파일하는 데만 쓰고, 실행은 항상 Django 커서로 합니다. 그래서 `atomic()`, savepoint, `on_commit`, `execute_wrapper`, `assertNumQueries`, Django 연결 풀, PgBouncer 설정이 그대로 동작합니다.
  - SQLAlchemy `Connection`·ORM `Session`은 Django 커서를 감싼 가짜 DBAPI 연결 위에서도 쓰지 않습니다(#39 에서 실험). 그 경로에서는 SQLAlchemy 의 `commit()`·`rollback()`·`begin()`이 에러 없이 아무 일도 하지 않고, SQLAlchemy 타입 변환이 돌아 시간대가 9시간 밀리거나 Enum 이 이름으로 저장되며(SQLite 에서는 Django 가 쓴 datetime 을 읽지도 못함), dialect 의 연결 훅이 Django 연결을 바꿉니다(SQLite 의 `FLOOR`·`REGEXP` 함수 교체, PostgreSQL notice handler 누적). Oracle 은 첫 연결부터 실패합니다.
- **SQLAlchemy 공개 API를 우선합니다.** 공개 API만으로 안 되는 부분(외부에서 받은 행으로 SQLAlchemy `Result`를 만드는 경로)은 어댑터 모듈 한 곳에 가두고, 지원하는 SQLAlchemy 버전 범위를 좁게 고정해 CI로 지킵니다.
- **테이블 구조의 기준은 Django 모델 하나입니다.** `sa.Table`은 Django 모델에서 만들고, 마이그레이션은 Django가 맡습니다.
- **값 변환은 Django 필드를 따릅니다.** 컬럼과 비교하는 값은 ORM 조회(`filter()`)처럼, 쓰는 값은 ORM 저장처럼(`get_db_prep_save`), 읽을 때는 Django ORM이 쓰는 변환기를 거칩니다. 그래서 JSON·UUID·시간대가 있는 datetime·Decimal·Enum이 ORM과 똑같이 왕복합니다. (SQLAlchemy 타입 변환을 그대로 쓰면 SQLite에서 시간이 9시간 밀리거나 Enum이 값 대신 이름으로 저장되는 문제가 있어 피합니다.)
- **경계를 드러냅니다.** 브리지로 쓴 내용은 Django signal을 발생시키지 않습니다. API 이름과 문서에 이 점을 분명히 적습니다.

## 모델에서 테이블 만들기

```python
import sqlalchemy as sa
from django_sqlalchemy_bridge import assert_table_matches, table_for

from shop.models import Order

orders = table_for(Order)  # 모델마다 한 번 만들고 캐시, 공용 MetaData 하나
stmt = sa.select(orders.c.id).where(orders.c.status == "paid")

# 테스트에서: 만든 테이블이 현재 모델·실제 DB 와 어긋나면 차이를 모두 나열하며 실패
assert_table_matches(Order, check_db=True)
```

- 테이블명은 `db_table`, 컬럼명은 `db_column`(FK 는 `<이름>_id`)을 따르고, PK·nullable·FK 대상 컬럼을 반영합니다.
- 각 `Column.info["django_field"]`에 원래 Django 필드가, `Table.info["django_model"]`에 모델이 들어 있습니다. 컬럼 타입은 아래 표의 SQLAlchemy 타입을 감싼 `FieldType`(`TypeDecorator`)이고 `column.type.impl`이 표의 타입, `column.type.field`가 Django 필드입니다. SQLAlchemy 타입은 SQL 컴파일에만 쓰고, 값 변환은 Django 필드가 맡습니다(아래 "실행하기").
- `compare_table(model, table=None, check_db=False)`는 차이를 `Difference(source, kind, column, expected, actual)` 리스트로 돌려줍니다. 모델과는 테이블명·컬럼·타입(`FieldType`이면 감싼 타입)·nullable·PK·FK 를, 실제 DB 와는 컬럼 유무·nullable·컬럼 타입을 비교합니다. 타입은 모델 필드의 `db_type()`(FK 는 대상 컬럼 타입)과 DB 컬럼 타입을 같은 표기로 맞춘 뒤 비교하고, 다르면 `Difference("db", "type", 컬럼, DB 타입, 모델이 바라는 타입)`을 냅니다(예: `"character varying(50)"` 대 `"character varying(100)"`).
  - PostgreSQL: `format_type()`이 주는 이름으로 맞춥니다(`varchar`→`character varying`, `serial`·`int4`→`integer`, `timestamptz`→`timestamp with time zone`, `decimal`→`numeric` 등). varchar 길이와 `numeric(p,s)` 정밀도까지 비교합니다.
  - SQLite: 테이블에 선언된 타입 텍스트를 대소문자·공백만 맞춰 비교합니다. Django 가 만든 테이블은 선언 타입이 곧 `db_type()`입니다. Django 가 `DecimalField`를 정밀도 없이 `decimal`로 선언하므로 SQLite 에서는 decimal 정밀도 차이를 잡지 못하고, 손으로 만든 테이블이 다른 동의어(`INT` 등)를 썼다면 타입 차이로 보고합니다.
  - MySQL: `information_schema.columns`의 `COLUMN_TYPE`과 맞춥니다(`integer`→`int`, `bool`→`tinyint(1)`, `numeric`→`decimal`, `double precision`·`real`→`double`, `AUTO_INCREMENT`와 정수 표시 폭 `int(11)`은 뗌). varchar 길이, `decimal(p,s)` 정밀도, `datetime(6)`의 소수 자릿수, `unsigned`까지 비교합니다.
  - 그 밖의 백엔드(MariaDB: JSON 이 `longtext`, UUID 가 고유 타입. Oracle: 데이터 사전이 `db_type()`과 다르게 씀, 예를 들어 `DOUBLE PRECISION`을 `FLOAT(126)`으로)에서는 타입을 비교하지 않습니다.
  - Oracle 에서는 컬럼 이름을 Django 가 실제로 만든 이름(대문자, 30자를 넘으면 해시로 줄임)으로 맞춰 보고, nullable 은 Django 스키마 편집기의 규칙대로 봅니다. Oracle 은 빈 문자열을 `NULL`로 저장하므로 Django 가 빈 문자열을 받는 필드(`CharField`·`TextField` 등, PK 제외)를 `null=False`여도 NULL 허용 컬럼으로 만들고, `compare_table`도 그 컬럼을 NULL 허용으로 기대합니다.
- 테이블은 모델에서 만들고 어느 DB 에도 묶이지 않습니다. 어느 DB 에서 실행할지는 호출의 `using`이 정합니다(아래 "다중 DB").
- `forget_table(model)`은 캐시와 공용 `MetaData`에서 테이블을 지웁니다. 테스트(`isolate_apps` 등)에서 동적 모델을 정리하는 용도입니다.

### 지원 필드

| Django 필드 (`get_internal_type()` 기준) | SQLAlchemy 타입 |
|---|---|
| `AutoField`, `IntegerField`, `PositiveIntegerField` | `Integer` |
| `BigAutoField`, `BigIntegerField`, `PositiveBigIntegerField` | `BigInteger` |
| `SmallAutoField`, `SmallIntegerField`, `PositiveSmallIntegerField` | `SmallInteger` |
| `CharField`(`EmailField`·`URLField` 포함), `SlugField`, `FileField`(`ImageField` 포함), `FilePathField` | `String(max_length)` |
| `GenericIPAddressField` | `String(39)` |
| `TextField` | `Text` |
| `BooleanField` | `Boolean` |
| `DecimalField` | `Numeric(max_digits, decimal_places)` |
| `FloatField` | `Float` |
| `DateField` / `TimeField` | `Date` / `Time` |
| `DateTimeField` | `DateTime(timezone=USE_TZ)` |
| `DurationField` | `Interval` |
| `UUIDField` | `Uuid` |
| `JSONField` | `postgresql.JSONB` (SQLite·MySQL·Oracle 에서는 `JSON` 으로 컴파일) |
| `BinaryField` | `LargeBinary` |
| `ForeignKey`, `OneToOneField` | 대상 컬럼의 타입 + `ForeignKey` |

- `choices`가 있는 필드는 바탕 필드의 타입을 그대로 씁니다.
- `JSONField`는 `postgresql.JSONB().with_variant(sa.JSON(), "sqlite", "oracle")`에 MySQL·MariaDB 용 `JSON` 변형을 붙인 타입입니다(SQLAlchemy 2.0 의 Oracle dialect 에는 JSON 이 없어 2.0 에서는 Oracle 변형을 붙이지 않습니다). 연산자(`contains()`·`has_key()` 등)는 dialect 를 고르기 전, 식을 만들 때의 타입에서 나오므로 기본 타입이 `JSONB`여야 PostgreSQL 연산자가 열립니다(`sa.JSON` 기본에 PostgreSQL 변형만 붙이면 `contains()`가 조용히 `LIKE`가 됩니다). 그래서 `compare_table`은 직접 만든 테이블의 `sa.JSON()` 컬럼을 타입 차이로 보고합니다.
- `ManyToManyField`와 역관계는 컬럼이 아니므로 건너뜁니다(중간 테이블 모델은 따로 `table_for` 할 수 있습니다). 다중 테이블 상속의 자식 테이블에는 자기 컬럼과 `<부모>_ptr_id`만 들어갑니다. 복합 PK(`CompositePrimaryKey`)를 반영합니다.
- 위 표에 없는 필드가 DB 컬럼을 가지면(예: PostgreSQL `ArrayField`, 직접 만든 필드) 조용히 넘기지 않고 `UnsupportedFieldError`를 냅니다.

## 실행하기

```python
import sqlalchemy as sa
from django.db import transaction
from django_sqlalchemy_bridge import execute, execute_result, table_for

from shop.models import Order

orders = table_for(Order)

with transaction.atomic():  # 브리지 쿼리도 이 트랜잭션 안에서 돈다
    rows = execute(sa.select(orders.c.id, orders.c.paid_at).where(orders.c.status == "paid"))
    # [(1, datetime.datetime(2026, 10, 25, 0, 30, tzinfo=datetime.timezone.utc)), ...]

    n = execute(sa.update(orders).where(orders.c.id.in_([1, 2])).values(status="shipped"))  # rowcount

    stmt = sa.update(orders).where(orders.c.id == sa.bindparam("pk")).values(status=sa.bindparam("s"))
    execute(stmt, [{"pk": 1, "s": "paid"}, {"pk": 2, "s": "void"}])  # cursor.executemany

result = execute_result(sa.select(orders.c.id, orders.c.status))  # SQLAlchemy Result (옵트인)
for row in result.mappings():
    ...
```

- `execute(stmt, params=None, *, using="default")`는 `connections[using]`의 vendor에 맞는 SQLAlchemy dialect(SQLite, PostgreSQL+psycopg, MySQL·MariaDB+mysqlclient, Oracle+oracledb)로 컴파일하고 **Django 커서로** 실행합니다. 그래서 `atomic()`·savepoint·`on_commit`·`execute_wrapper`·`CaptureQueriesContext`·`assertNumQueries`가 ORM 쿼리와 똑같이 적용됩니다.
- **반환 형태**: 행을 돌려주는 문장(`SELECT`, `RETURNING`)은 튜플 리스트, 그 밖의 문장은 커서의 rowcount(정수)입니다. `params`에 매핑을 넘기면 `bindparam()` 값(그리고 `insert(table)`처럼 값이 없는 INSERT의 컬럼)을 채우고, 매핑의 시퀀스를 넘기면 `cursor.executemany()`로 실행합니다(이때 `RETURNING`은 받지 않습니다).
- `execute_result(stmt, params=None, *, using="default")`는 같은 행을 SQLAlchemy `Result`로 줍니다(`row.name`, `row._mapping`, `.mappings()`, `.scalars()`). 행을 모두 가져와 변환한 뒤의 `Result`입니다. 외부 행으로 `Result`를 만드는 공개 API가 없어 이 부분만 `_sa_result.py` 한 곳에서 비공개 API를 씁니다. 키는 DB 가 알려 주는 컬럼 이름입니다. Oracle 은 이름을 대문자(브리지가 Django 식으로 쓴 이름)로 돌려주므로, 문장의 컬럼·라벨이면 문장을 만들 때의 이름(`MixedCase`, 30자 넘는 이름, `label("Who")`)으로 되돌리고, 그 밖의 이름(`sa.text()`의 컬럼, `count_1` 같은 SQLAlchemy 익명 라벨)은 SQLAlchemy 의 Oracle dialect 처럼 전부 대문자면 소문자로, 아니면 받은 그대로 둡니다. 그래서 `text('SELECT name AS Plain, name AS "MixedCase" ...')`의 키는 `plain`·`MixedCase`입니다. 다른 DB 의 키는 바꾸지 않습니다(테스트로 확인).
- **값 변환은 Django 필드 기준**입니다. 컬럼과 비교(`==`, `!=`, `<`, `IN`, `BETWEEN` 등)하는 값은 ORM 조회 `filter(field=v)`와 같은 경로(`field.get_prep_value()` 뒤 `get_db_prep_value(prepared=True)`, FK 는 대상 필드)를, INSERT/UPDATE로 넣는 값은 ORM 저장과 같은 `field.get_db_prep_save()`를, 결과 컬럼은 ORM과 같은 변환기(`connection.ops.get_db_converters()` + 변환기)를 거칩니다. 이때 ORM 처럼 테이블 컬럼과 식을 가릅니다. 테이블 컬럼(라벨·서브쿼리·CTE·UNION 첫 SELECT 를 거친 것 포함)은 ORM 의 컬럼 읽기(`Col`)와 같고, 컬럼 타입을 잇는 식(`sum()`·`max()`·`coalesce()`·`price * 3`·`price + price`·`case()`·`over()`·스칼라 서브쿼리, 그리고 그런 식을 담은 서브쿼리·CTE 의 컬럼)은 ORM 의 `Func(output_field=필드)` 읽기와 같습니다. 차이는 SQLite 의 `DecimalField` 에서 드러납니다. 컬럼은 필드의 `decimal_places` 로 맞추고(`Decimal('30.00')`), 식은 15 유효자리로만 반올림해 REAL 계산의 부동소수 오차를 걷어낼 뿐 자릿수를 맞추지 않습니다(평균 `Decimal('30.3333333333333')`, 합 `Decimal('91')`). 그래서 필드의 `max_digits` 를 넘는 합도 읽힙니다(#68 전에는 식도 컬럼처럼 읽어 평균이 `30.33` 으로 잘리고 이런 합은 `decimal.InvalidOperation`). 자릿수가 필요하면 읽은 뒤 `quantize()` 하세요. 다른 DB 는 드라이버가 준 `Decimal` 그대로라 두 읽기가 같습니다. JSON·UUID·시간대 있는 datetime(SQLite에서도 UTC로 저장, 9시간 밀리지 않음)·Decimal·선택지(이름이 아니라 값)가 ORM과 똑같이 왕복합니다. `None`은 ORM `save()`처럼 SQL `NULL`입니다. 비교와 쓰기를 가르는 기준은 자리입니다. 비교 연산자의 피연산자(`IN` 목록·`BETWEEN` 경계·튜플 항목 포함)로 바로 들어간 값이 조회 값이고, 나머지(`VALUES`·`SET`, 산술·함수 인자 안의 값)는 쓰기 경로입니다. 튜플 비교의 값 쪽은 `tuple_(a, b) == (x, y)`·`.in_([(x, y)])`처럼 평범한 튜플로 쓰세요. `tuple_(a, b) == tuple_(x, y)`처럼 값 쪽도 `tuple_()`로 감싸면 그 값은 SQLAlchemy 가 값에서 추론한 타입이라 변환 없이 가서, SQLite 에서 시간대 있는 datetime 이 맞지 않습니다(에러 없이 0행). 두 경로는 백엔드가 쓰는 값만 검사할 때 갈립니다. 예를 들어 MySQL은 `AutoField`에 0을 쓰지 못하므로 `id == 0` 비교는 ORM처럼 실행되고 `id=0` INSERT는 ORM처럼 `ValueError`입니다.
- **SQLite 의 `DecimalField` 식은 ORM 처럼 `NUMERIC` 으로 캐스트합니다.** Django 는 SQLite 에서 `DecimalField` 값을 텍스트로 묶는데, 컬럼은 NUMERIC 친화성이 있어 텍스트를 숫자로 비교하지만 `SUM()`·산술·`CASE` 의 결과는 친화성이 없어 숫자가 늘 텍스트보다 작습니다(`SUM(price) >= '51'` 이 모든 행에서 거짓). ORM 은 출력 필드가 `DecimalField` 인 함수·산술·`CASE`·윈도를 `(CAST(... AS NUMERIC))` 으로 감싸고(`SQLiteNumericMixin`), 브리지 SQLite dialect 도 타입이 `DecimalField` 컬럼의 타입인 식(`sum()`·`max()`·`min()`·`coalesce()`·`price + price`·`-price`·`case()`·`over()`·`filter()`)을 같은 자리에서 감쌉니다(윈도·`FILTER` 는 그 바깥). 컬럼 자체는 감싸지 않습니다. 다른 DB 의 SQL 은 그대로입니다. SQLAlchemy 의 `func.avg()` 는 타입이 없으므로 `sa.func.avg(books.c.price, type_=books.c.price.type)` 처럼 타입을 주어야 감싸지고 `Decimal` 로 읽힙니다.
- 컬럼으로 추적되지 않는 값, 곧 SQLAlchemy 가 컬럼 타입을 붙이지 않는 값(`LIKE` 패턴, `count()` 처럼 타입이 따로인 식, `sa.literal(v)`·`sa.bindparam("v")` 처럼 타입 없는 값, `sa.text()`)은 변환 없이 그대로 오갑니다. 산술식은 컬럼 타입을 이으면(`price * 3`, `price + price`) 위처럼 변환되고, 타입 없는 리터럴끼리의 산술만 여기에 듭니다. `INSERT … SELECT`의 SELECT 목록에 넣는 값도 여기에 듭니다. 브리지는 그 값이 들어갈 대상 컬럼을 알지 못하므로(SQLAlchemy 가 `from_select()`의 컬럼 이름을 공개 API 로 내주지 않음) 값마다 대상 컬럼 타입을 주세요: `sa.literal(value, daily_stock.c.counted_at.type)`, `sa.bindparam("at", type_=daily_stock.c.counted_at.type)`. 타입이 없으면 UUID(SQLite)·JSON 컬럼에 넣는 dict(PostgreSQL)는 드라이버에서 에러가 나고, 시간대 있는 datetime 은 SQLite 에서 에러 없이 UTC 가 아닌 오프셋 문자열로 저장돼 ORM 필터가 그 행을 찾지 못합니다. MySQL·Oracle 에서는 드라이버(mysqlclient·oracledb)가 오프셋을 버리고 벽시계 시각을 저장해, Django 가 UTC 로 읽으면 다른 시각(서울 시각이면 9시간 뒤)이 됩니다. 예외는 JSON 키·경로(`meta["k"]`, `meta[0]`, `meta[("a", "b")]`)로, 값이 아니라 문법이라 dialect 표기(SQLite `'$."k"'`, PostgreSQL `'{a, b}'`)로 바꿔 넘깁니다.
- **JSON 컬럼.** `meta["k"] == 1`(ORM `meta__k=1`), `meta[("a", "b")] == 1`, `meta == {...}`, `meta["k"].in_([...])`, `meta["k"].as_string()`은 세 백엔드에서 동작하고 결과가 ORM 조회와 같습니다(SQLite 의 구조 값 비교는 아래 예외). PostgreSQL에서는 jsonb 연산자도 쓸 수 있고 결과가 ORM 조회와 같습니다(테스트로 확인).

  | 브리지 (PostgreSQL) | ORM |
  |---|---|
  | `meta.contains({"k": 1})` (`@>`) | `meta__contains={"k": 1}` |
  | `meta.contained_by({...})` (`<@`) | `meta__contained_by={...}` |
  | `meta.has_key("k")` / `has_all([...])` / `has_any([...])` | `meta__has_key` / `meta__has_keys` / `meta__has_any_keys` |
  | `meta["k"].astext == "1"` (`->>`) | `annotate(v=KT("meta__k")).filter(v="1")` |

  `contains`·`contained_by`의 우변은 컬럼 값과 같게 `JSONField.get_db_prep_value()`로 jsonb 바인딩합니다. SQLite에는 이 연산자들이 없습니다. `contains`·`has_key`는 실행 시 `OperationalError`, `astext`(`->>`)는 SQLite 3.38+의 `->>`로 실행되지만 텍스트가 아니라 SQL 값을 돌려줘 `{"k": 1}`이 `'1'`과 같지 않습니다. SQLite에서 `meta["k"] == [1, 2]`처럼 구조 값과 비교하면 JSON 텍스트끼리 비교라 공백이 다르면 맞지 않습니다(ORM 은 `JSON()`으로 정규화). 스칼라 비교는 ORM 과 같습니다.

  MySQL 은 JSON 값과 문자열을 비교하면 문자열을 JSON 문자열로 보므로(`'1'`이 `{"k": "1"}`에 맞음), 브리지는 ORM 처럼 JSON 컬럼에 묶는 값을 `JSON_EXTRACT(%s, '$')`로 감싸 JSON 문서로 읽힙니다(INSERT·UPDATE 로 넣는 값도 같은 문서로 저장됩니다). `contains`·`has_key`는 MySQL 에 없는 연산자라 실행 시 `ProgrammingError`이고, `astext`는 MySQL 의 `->>`(`JSON_UNQUOTE(JSON_EXTRACT(...))`)라 `{"k": 1}`과 `{"k": "1"}` 모두 텍스트 `'1'`입니다(ORM `KT()` 필터는 MySQL 에서 우변을 JSON 으로 감싸 텍스트와 맞지 않으니 결과를 대조하지 마세요).

  Oracle 에서 Django 는 `JSONField`를 `NCLOB`(`IS JSON`)으로 저장하는데, Oracle 은 `NCLOB`을 `=`로 비교하지 못하고(`ORA-22848`) 키는 `JSON_QUERY()` 텍스트라 `[1,2]`가 Django 가 쓴 `[1, 2]`와 맞지 않습니다. 그래서 브리지는 JSON 끼리의 `==`·`!=`를 `JSON_EQUAL()`로 컴파일합니다. 문서 전체 비교(`meta == {...}`)는 ORM 과 같습니다. 키 비교는 JSON 값으로 비교해 `meta["k"] == 1`이 `{"k": "1"}`에 맞지 않습니다(PostgreSQL·MySQL 과 같음). Oracle 의 ORM 은 키를 `JSON_VALUE()` 텍스트로 비교해 `1`과 `"1"`이 서로 맞으니 결과를 대조하지 마세요. `meta["k"].in_([...])`는 JSON 텍스트 비교라 구조 값이 맞지 않습니다(SQLite 와 같음). `as_string()`·`as_integer()` 등은 SQLAlchemy 가 `JSON_VALUE(... ERROR ON ERROR)`로 컴파일해 키가 없는 행 하나에 문장 전체가 실패하므로, 브리지는 `ERROR ON ERROR`를 떼어 다른 DB 처럼 `NULL`을 받게 합니다. `contains`·`has_key`는 Oracle 에 없는 연산자입니다. SQLAlchemy 2.0 의 Oracle dialect 는 JSON 키·경로를 컴파일하지 못해 실행 전 `sqlalchemy.exc.UnsupportedCompilationError`입니다(문서 전체 비교는 됩니다). JSON 키·경로를 Oracle 에서 쓰려면 SQLAlchemy 2.1 을 쓰세요.
- **컴파일 캐시: `bindparam()`으로 만든 문장을 재사용하면 컴파일을 건너뜁니다.** 브리지는 넘겨받은 문장 객체마다 컴파일 결과를 기억해, 같은 객체를 같은 DB 종류(vendor)에서 같은 이름의 `params`로 다시 실행하면 컴파일 없이 값 변환과 실행만 합니다(`execute`·`execute_result`·`stream` 공통). 그러니 값은 `sa.bindparam("pk")`로 비워 둔 문장을 한 번 만들어 두고(모듈 수준 등) 돌려 쓰세요. `where(orders.c.id == pk)`처럼 값을 넣어 매번 새로 만든 문장은 매번 컴파일합니다(아래 "성능과 메모리" 4번). `params` 이름이 달라지면(예: UPDATE 의 `SET` 컬럼) SQL 이 달라지므로 따로 컴파일합니다. `RETURNING` 지원 검사는 연결(alias)마다 다를 수 있어 매 호출 그 연결로 다시 정하고, 결과 변환기는 연결 객체(`connections[alias]`, 스레드마다 하나)에 컬럼 타입마다 테이블 컬럼용과 식용을 따로 한 번 찾아 둡니다. 기억한 결과는 문장 객체가 사라지면 함께 사라지고, 여러 스레드가 같은 문장을 써도 됩니다. 끄는 스위치는 없습니다(결과가 캐시 여부와 무관하므로).
- 리터럴 `%`는 그대로 써도 됩니다(`LIKE 'a%'`, `text("SELECT '100%'")`). 브리지가 `%s` 자리표시로 컴파일하고 `%`를 `%%`로 바꾼 뒤, 파라미터가 없어도 빈 튜플을 넘겨 백엔드가 되돌리게 합니다(MySQL 의 mysqlclient 도 같은 format paramstyle 입니다). `sa.text()`는 그 DB 의 SQL 그대로라, MySQL 기본 `sql_mode`에서는 문자열 리터럴의 백슬래시가 이스케이프입니다(`ESCAPE '\\'`로 써야 백슬래시 하나).
- async 코드에서 직접 부르면 ORM처럼 `SynchronousOnlyOperation`이 납니다. 작업 단위를 동기 함수로 묶어 `arun`으로 부르세요(아래 "비동기").
- 모델 인스턴스가 필요하면 `raw(Model, stmt)`가 Django `RawQuerySet`을 줍니다(아래 "모델 인스턴스로 읽기").
- DRF·클래스 기반 뷰(`ViewSet`·`APIView`·페이지네이션·`ModelSerializer`·`TemplateView`)에서 쓰는 레시피와 되지 않는 것(`ModelViewSet`·`DetailView`·필터 백엔드·`CursorPagination` 등)은 [docs/drf.md](docs/drf.md)에 있습니다.

### 쪼개 읽기: `stream`

```python
from django_sqlalchemy_bridge import stream

for order_id, paid_at in stream(sa.select(orders.c.id, orders.c.paid_at), chunk_size=2000):
    ...
```

- `stream(stmt, params=None, *, chunk_size=2000, using="default")`는 행을 `chunk_size`개씩 가져와 튜플로 하나씩 내주는 제너레이터입니다. 값 변환은 `execute`와 같습니다. 기본 `chunk_size`는 Django `QuerySet.iterator()`의 기본값(2000)과 같습니다.
- 커서는 `QuerySet.iterator()`와 같은 경로로 엽니다. `connection.chunked_cursor()`(PostgreSQL에서는 서버 측 named cursor, SQLite·MySQL·Oracle에서는 일반 커서)에 `fetchmany(chunk_size)`를 반복합니다. MySQL 의 일반 커서는 mysqlclient 가 `execute()` 때 결과 전체를 클라이언트로 받으므로 `QuerySet.iterator()`처럼 메모리 이득이 없습니다(`atomic()` 안팎 모두, 테스트로 확인). 변환한 튜플만 청크 단위로 만듭니다. 데이터베이스 설정에 `DISABLE_SERVER_SIDE_CURSORS: True`가 있으면 일반 커서를 씁니다. `CaptureQueriesContext`·`assertNumQueries`에는 `iterator()`처럼 1건으로 잡힙니다(`FETCH`는 잡히지 않습니다).
- 첫 행을 요청할 때 실행됩니다. 행이 끝나거나, 제너레이터를 `close()`하거나(`for` 문을 `break`로 빠져나가 제너레이터가 버려질 때 포함), 예외로 끝나면 커서를 닫습니다. 제너레이터를 변수에 쥔 채 소비를 멈추면 닫히지 않으니 `close()`를 부르거나 `contextlib.closing()`으로 감싸세요.
- 행을 돌려주지 않는 문장(`RETURNING` 없는 DML)은 실행 전에 `ValueError`, 매핑의 시퀀스(executemany 형태)는 `TypeError`입니다. 행이 없는 `text()` 문장은 첫 `next()`에서 `ValueError`입니다.
- PostgreSQL의 서버 측 커서(`DECLARE`)는 `SELECT`·`VALUES`만 받습니다. 그래서 `RETURNING`이 있는 DML은 일반 커서로 실행하고(행은 드라이버가 한꺼번에 받으므로 메모리 이득은 없습니다), `text()` 문장도 `SELECT`·`VALUES`여야 합니다. 데이터를 바꾸는 CTE(`WITH u AS (UPDATE ... RETURNING ...) SELECT ...`)는 PostgreSQL이 `NotSupportedError`로 거부하니 `execute`를 쓰세요.

**PostgreSQL에서 트랜잭션 밖(autocommit)일 때.** Django는 autocommit 상태에서 named cursor를 `WITH HOLD`로 엽니다(`atomic()` 안에서는 일반 커서). 저장소에서 확인한 동작은 이렇습니다(PostgreSQL 18, psycopg 3).

- `WITH HOLD` 커서는 `DECLARE`가 커밋되는 시점에 **결과 전체를 서버에서 계산해 서버 쪽에 저장합니다.** 그래서 첫 실행이 쿼리 전체만큼 걸리고(이후 `fetchmany`는 저장된 결과를 읽습니다), 결과가 크면 서버의 임시 저장 공간을 그만큼 씁니다. 클라이언트 메모리는 여전히 청크 단위입니다. `atomic()` 안에서는 결과를 미리 만들지 않고 `fetchmany`마다 계산합니다.
- 커서는 그 뒤 다른 트랜잭션이 커밋돼도 살아 있고, 만들 때의 결과를 그대로 줍니다. 닫지 않으면 연결이 닫힐 때까지 서버에 남습니다.
- 반대로 `atomic()` 안에서 연 스트림은 그 트랜잭션이 끝나면 서버 커서가 사라집니다. 블록을 나온 뒤 계속 소비하면 `ProgrammingError: cursor ... does not exist`가 납니다. 블록 안에서 다 읽으세요.
- PgBouncer를 transaction pooling 모드로 쓰면 트랜잭션이 끝난 뒤의 `FETCH`가 커서를 연 것과 다른 서버 연결로 갈 수 있으니, Django 문서대로 `DISABLE_SERVER_SIDE_CURSORS: True`를 설정해야 합니다. 그러면 `stream`도 일반 커서를 씁니다(드라이버가 결과를 한꺼번에 받습니다).

### 모델 인스턴스로 읽기: `raw`

```python
from django_sqlalchemy_bridge import raw

paid = sa.select(orders).where(orders.c.status == sa.bindparam("status"))
for order in raw(Order, paid, {"status": "paid"}):  # Order 인스턴스. 쿼리는 여기서 나간다
    order.customer  # 평소의 모델 인스턴스: 관계·메서드·ModelSerializer 가 그대로
```

- `raw(model, stmt, params=None, *, using="default")`는 Django `RawQuerySet`을 돌려줍니다. 브리지는 문장을 `using` 연결의 dialect 로 컴파일하고 `params`를 `execute`와 같이 Django 필드 기준으로 변환(컬럼과 비교하는 값은 ORM 조회처럼)한 뒤 `model._default_manager.db_manager(using).raw(sql, params)`를 부릅니다. 컴파일 캐시도 `execute`와 같이 탑니다.
- **게으릅니다.** 컴파일·변환·검사는 `raw()`를 부를 때 하지만 SQL 은 `RawQuerySet`을 반복할 때 Django 커서로 나갑니다(`connection.queries`·`execute_wrapper`·`assertNumQueries`에 1건). 한 번 반복한 `RawQuerySet`은 결과를 기억하고, `prefetch_related()`·`using()`·`async for`도 Django 의 것 그대로입니다.
- **대상 DB 는 `raw(..., using=)`로 고르세요.** SQL 과 `params`는 `raw()`를 부를 때 그 `using` 연결 기준으로 정해집니다. 돌려받은 `RawQuerySet`의 `.using("other")`는 Django 의 것이라 같은 SQL·값을 다른 연결로 옮길 뿐 다시 컴파일·변환하지 않으므로, dialect 나 값 변환(`TIME_ZONE` 등)이 다른 DB 에서는 에러 없이 결과가 틀릴 수 있습니다(`TIME_ZONE`이 다른 SQLite alias 에서 `raw(Author, stmt, using="other")`는 1행, `raw(Author, stmt).using("other")`는 0행, 테스트로 고정).
- **결과 변환은 Django 가 합니다.** Django `raw()`의 계약 그대로, 결과 컬럼 이름이 필드의 컬럼 이름(`db_column`)과 같으면 그 필드에 ORM 이 읽을 때의 변환기를 거쳐 들어갑니다(Decimal·UUID·JSON·시간대 있는 datetime·choices 가 `Model.objects.get()`과 같은 값). 문장에서 뺀 필드는 deferred 로, 처음 읽을 때 쿼리가 한 번 더 나갑니다. 필드가 아닌 컬럼(`label("n_sales")`)은 인스턴스 속성이 되며 **변환하지 않은 드라이버 값**입니다. 예를 들어 `sa.func.max(sales.c.sold_at).label("last_sold")`는 SQLite 에서 저장된 텍스트(`str`), 다른 DB 에서 `datetime`입니다(`execute`는 이런 라벨도 컬럼 타입으로 변환하지만 `raw()`는 Django 에 맡깁니다). 그런 값이 필요하면 `execute_result`로 읽으세요.
- **PK 가 있어야 합니다.** `select()`처럼 컬럼 이름을 컴파일 때 아는 문장은 모델 PK 의 컬럼이 없으면 실행 전에 `ValueError`입니다. `sa.text()`는 이름을 알 수 없어 Django 에 맡기고, Django 가 반복할 때 `FieldDoesNotExist`를 냅니다.
- 행을 고르는 문장(`select()`·`union_all()` 같은 복합 SELECT·`sa.text()`)만 받습니다. `insert`·`update`·`delete`는 `RETURNING`이 있어도 `ValueError`, `params`에 매핑의 시퀀스를 넘기면 `ValueError`입니다(executemany 는 뜻이 없음). 파라미터가 없어도 빈 튜플을 넘기므로 리터럴 `%`는 `execute`처럼 그대로 씁니다.
- Oracle 에서 Django `raw()`는 커서가 알려 주는 컬럼 이름을 소문자로 바꿔 필드 컬럼과 맞춥니다. 브리지는 Django 식 철자(대문자)로 쓰므로 `db_column="MixedCase"`는 맞지만, 30자를 넘는 컬럼은 Oracle 에서 해시로 줄인 이름이라 어느 필드와도 맞지 않아 deferred 가 되고 줄인 이름의 속성이 붙습니다(Django `Model.objects.raw()`도 같음, 테스트로 확인).
- **비동기.** `async for obj in qs`는 Django `RawQuerySet`의 비동기 반복 그대로입니다. 다만 `raw()`는 부를 때 컴파일하는데, MySQL 은 dialect 를 고르려고 연결의 첫 호출에서 서버 정보(MariaDB 여부, `sql_mode`)를 읽으므로 async 코드에서 바로 부르면 `SynchronousOnlyOperation`입니다. `qs = await sync_to_async(raw)(Model, stmt)`로 만드세요(다른 DB 는 바로 불러도 됩니다).
- 복합 PK(Django 5.2 `CompositePrimaryKey`)는 검사가 모든 PK 컬럼을 보지만 테스트하지 않았습니다.

### 여러 문장 한 번에: `execute_pipeline`

```python
from django_sqlalchemy_bridge import execute_pipeline

by_pk = sa.select(orders.c.id, orders.c.status).where(orders.c.id == sa.bindparam("pk"))
mark = sa.update(orders).where(orders.c.id == sa.bindparam("pk")).values(status="seen")
results = execute_pipeline(
    [
        *[(by_pk, {"pk": pk}) for pk in (1, 2, 3)],
        sa.select(sa.func.count()).select_from(orders),  # params 가 없으면 문장만
        (mark, [{"pk": 1}, {"pk": 2}]),  # executemany
    ]
)
# [[(1, "paid")], [(2, "void")], [(3, "paid")], [(42,)], 2]
```

- `execute_pipeline(items, *, using="default")`의 항목은 문장 또는 `(문장, params)` 쌍입니다. 항목마다 의미와 결과는 `execute`와 같고(행 튜플 리스트 또는 rowcount, 값 변환, `text()`, 매핑의 시퀀스는 executemany), 결과는 입력 순서의 리스트입니다. 빈 입력은 `[]`이고 아무것도 실행하지 않습니다.
- **PostgreSQL + psycopg 3 + 파이프라인을 지원하는 libpq(14 이상)** 에서는 psycopg 의 파이프라인 모드(`connection.connection.pipeline()`) 안에서 문장마다 Django 커서로 실행해, 모든 문장을 보낸 뒤 결과를 읽습니다. 왕복이 문장 수만큼이 아니라 한 번이라 독립 조회가 많을수록, 서버가 멀수록 빨라집니다(아래 "성능과 메모리" 8). 그 밖(SQLite·MySQL·Oracle·psycopg2)에서는 같은 순서로 하나씩 실행해 같은 결과를 줍니다. 속도만 다릅니다.
- 항목 사이의 의존(앞 결과를 뒤 `params`로)은 받지 않습니다. 모든 문장이 호출할 때 준비돼 있어야 합니다.
- **실행 전 검사는 모든 항목에 대해 먼저 합니다.** `RETURNING`을 지원하지 않는 DB, SQLAlchemy Python 측 default, executemany 와 `RETURNING`처럼 `execute`가 실행 전에 거부하는 항목이 하나라도 있으면 아무것도 실행하지 않습니다.
- Django 커서를 지나므로 `connection.queries`·`CaptureQueriesContext`·`assertNumQueries`·`execute_wrapper`에 문장마다 하나씩(executemany 항목은 하나) 잡힙니다. 컴파일 캐시도 항목마다 그대로 탑니다.
- **문장을 둘 이상 실행하는 호출은 `atomic(using, savepoint=False)` 안에서 돕니다**(`bulk_*`와 같음). 그래서 `atomic()` 밖에서도, 모든 백엔드에서 한 호출의 쓰기는 모두 되거나 모두 안 됩니다. 트랜잭션 밖에서 부르면 Django 가 `BEGIN`·`COMMIT`을 쿼리 기록에 남기고, PostgreSQL 에서는 `COMMIT` 왕복이 하나 늘어납니다. 바깥 `atomic()` 안에서 이 호출의 에러를 잡고 계속하려면 호출을 `atomic()`으로 한 번 더 감싸세요(savepoint).
  - PostgreSQL 파이프라인은 그 자체로도 Sync 까지의 문장을 암묵적 트랜잭션으로 묶습니다. 저장소에서 감싸지 않고 확인했을 때 autocommit 에서 뒤 문장이 실패하면 앞 쓰기도 남지 않았습니다(PostgreSQL 18, psycopg 3.3). 다만 executemany 항목은 결과를 남기지 않아 어느 항목이 실패했는지 알려면 그 뒤에 Sync 를 하나 더 보내야 하고(항목마다 왕복 하나), 트랜잭션 밖의 Sync 는 커밋이라 브리지가 트랜잭션을 엽니다.
- **한 항목이 실패하면** 그 항목의 DB 에러(Django 의 `IntegrityError` 등)가 그대로 나고, 에러의 note(`e.__notes__`, 트레이스백 끝에 보임)에 `execute_pipeline(): item 2 failed`처럼 몇 번째 항목인지 적힙니다. psycopg 가 실패 뒤 문장에 내는 `PipelineAborted`는 드러나지 않습니다. 뒤 항목은 효과가 없습니다. PostgreSQL 은 이미 보낸 문장을 서버가 건너뛰고(그래서 `execute_wrapper`에는 보인 문장으로 잡힙니다), 하나씩 실행하는 백엔드는 보내지 않습니다. `atomic()` 안이면 블록이 롤백됩니다.
- 결과 하나짜리 `Result`(`execute_result`)판, `stream`의 파이프라인화, 결과 지연 읽기는 없습니다. async 코드에서는 다른 API 처럼 `SynchronousOnlyOperation`이니 `arun`으로 감싸세요.

### CSV 로 내보내기: `copy_to` (PostgreSQL)

```python
from django.http import StreamingHttpResponse
from django_sqlalchemy_bridge import copy_to

paid = sa.select(orders.c.id, orders.c.status, orders.c.paid_at).where(orders.c.status == sa.bindparam("s"))
response = StreamingHttpResponse(copy_to(paid, {"s": "paid"}), content_type="text/csv")

with open("orders.csv", "wb") as f:
    f.writelines(copy_to(paid, {"s": "paid"}))
```

- `copy_to(stmt, params=None, *, format="csv", header=True, using="default")`는 `COPY (<컴파일한 문장>) TO STDOUT WITH (FORMAT csv, HEADER …)`를 Django 커서 아래 psycopg 커서(`cursor.cursor.copy()`)로 실행하고 **서버가 쓴 바이트를 그대로** 내주는 제너레이터입니다. 행을 파이썬 객체로 만들지 않습니다. psycopg 는 COPY 데이터를 행 하나씩 주므로 브리지가 64 KiB 이상씩 모아 내줍니다(마지막 덩어리만 더 작을 수 있음). `StreamingHttpResponse`는 덩어리마다 한 번 쓰기 때문입니다.
- **값은 서버의 텍스트 표현입니다(Django 필드 변환 없음).** `timestamptz`는 Django 가 연결에 설정한 세션 시간대(`USE_TZ`면 UTC, `+00` 접미), boolean 은 `t`/`f`, numeric 은 그대로(`12.500`), jsonb 는 JSON 텍스트, bytea 는 `\x…`입니다. NULL 은 csv 에서 빈 칸, 빈 문자열은 `""`, text 포맷에서는 `\N`입니다. 쉼표·따옴표·개행·탭의 따옴표 처리와 이스케이프는 서버가 합니다(테스트로 고정).
- `format`은 `"csv"`(헤더 줄은 `header=True`가 기본, 컬럼 이름이나 `label()`)와 `"text"`(탭 구분, PostgreSQL 기본 COPY 포맷)입니다. text 포맷은 헤더를 받지 않아 `header=False`를 함께 줘야 하고, `header=True`면 `ValueError`입니다.
- `stmt`·`params`는 `execute`처럼 컴파일·변환합니다(컬럼과 비교하는 값은 ORM 조회처럼, 컴파일 캐시도 탐). PostgreSQL 의 COPY 는 프로토콜 파라미터를 받지 않으므로 브리지는 psycopg `ClientCursor.mogrify()`로 값을 SQL 리터럴로 쓴 뒤 파라미터 없이 COPY 합니다. psycopg 의 어댑터로 쓰므로 bytea·jsonb·UUID 도 그대로이고, Django `OPTIONS["server_side_binding"]`(psycopg `ServerBindingCursor`)에서도 같습니다. 파라미터가 없어도 거치므로 리터럴 `%`는 `execute`처럼 그대로 씁니다.
- 행을 고르는 문장(`select()`·복합 SELECT·`sa.text()`)만 받습니다. `insert`·`update`·`delete`는 `RETURNING`이 있어도 실행 전 `ValueError`, 매핑의 시퀀스는 `TypeError`입니다. PostgreSQL 이 아니거나 psycopg2 면 실행 전 `NotSupportedError`입니다.
- **`stream`과 같은 계약.** 첫 덩어리를 요청할 때 실행되고, 바이트가 끝나거나 `close()`하거나 예외로 끝나면 커서를 닫습니다. `atomic()` 안에서 열었으면 블록 안에서 끝까지 읽으세요. 일찍 `close()`하면 트랜잭션 밖(autocommit)에서는 psycopg 가 COPY 를 취소하고, **트랜잭션 안에서는 남은 데이터를 읽어 버립니다.** 취소하면 그 트랜잭션이 깨지기 때문입니다(결과가 클 때 `current transaction is aborted`, 테스트로 고정). 그래서 큰 내보내기를 트랜잭션 안에서 중간에 멈추면 나머지를 받는 시간만큼 걸립니다.
- 쿼리 기록은 `bulk_insert(method="copy")`와 같습니다. 디버그 기록이 켜져 있으면 값을 넣은 `COPY (…) TO STDOUT …` 문장 한 건이 `connection.queries`에 남고(시간은 바이트를 다 내준 뒤까지), **`execute_wrapper()`에는 보이지 않습니다.** 에러는 Django 예외(`ProgrammingError` 등)로 올라오고, 깨진 트랜잭션에서는 `execute`처럼 `TransactionManagementError`입니다.
- **언제 쓰나**: 많은 행을 그대로 파일·HTTP 로 내보낼 때. 200,000행(컬럼 7개)을 CSV 로 쓰는 데 `stream()` + `csv.writer` 766 ms 대 237 ms, tracemalloc 피크 40.0 MiB 대 12.3 MiB 였습니다(아래 "성능과 메모리" 9). 파이썬 값이 필요하거나 Django 식 표현(aware datetime 의 `+09:00`, `True`)이 필요하면 `stream`으로 읽어 `csv.writer`로 쓰세요(아래 "성능과 메모리" 9).

### 대량 쓰기: `bulk_update`, `bulk_insert`

```python
from django_sqlalchemy_bridge import bulk_insert, bulk_update

# PK(기본) 또는 지정한 키로 행마다 다른 값을 넣는 UPDATE. 반환은 rowcount
bulk_update(orders, [{"id": 1, "status": "paid"}, {"id": 2, "status": "void"}])
bulk_update(orders, rows, key="uid", columns=["status"])  # 키·설정 컬럼 지정, 복합 키는 key=("a", "b")

# INSERT. returning 을 주면 행마다 튜플(필드 변환을 거친 값), rows 순서대로
ids = bulk_insert(orders, rows, returning=["id"])

# 수십만 행 이상: batch_size 행씩 문장을 나눠 메모리를 줄인다(전부 쓰이거나 하나도 안 쓰임)
bulk_insert(orders, rows, batch_size=10_000)

# PostgreSQL 에서 RETURNING 이 필요 없는 수십만 행: COPY FROM STDIN
bulk_insert(orders, rows, method="copy")
```

- `bulk_update(table, rows, *, key=None, columns=None, batch_size=None, using="default")`, `bulk_insert(table, rows, *, returning=None, batch_size=None, using="default", method="insert")`. `table`은 `table_for()` 테이블이나 모델입니다. `rows`는 컬럼 이름(`db_column`, FK 는 `<이름>_id`)을 키로 한 매핑의 시퀀스입니다.
- **PostgreSQL에서는 문장 하나입니다.** 컬럼마다 배열 파라미터 하나를 Django 컬럼 타입으로 캐스트하고(`(%s)::jsonb[]`, `(%s)::numeric[]` …) `unnest()`로 행을 만듭니다. UPDATE 는 `UPDATE t SET c = v.c FROM unnest(...) AS v(...) WHERE t.k = v.k`, INSERT 는 `INSERT INTO t (...) SELECT * FROM unnest(...)` 꼴로, Django `bulk_create`가 PostgreSQL에서 쓰는 방식과 같습니다. 캐스트에서는 길이·자릿수를 떼므로(`varchar(50)[]`가 아니라 `varchar[]`) 값이 잘리지 않고, 너무 긴 값은 ORM 쓰기처럼 컬럼에 넣을 때 오류가 납니다. `CaptureQueriesContext`에는 1건으로 잡힙니다.
- **그 밖의 백엔드(SQLite·MySQL·Oracle)에서는 같은 호출이 `cursor.executemany()`입니다(`atomic()` 안에서, 아래 "원자성").** `returning`이 있는 INSERT 는 executemany 로 행을 받을 수 없어서 다중 `VALUES` 문장을 `connection.ops.bulk_batch_size()`(SQLite 변수 한도) 단위로 나눠 실행합니다. 결과는 PostgreSQL과 같습니다.
  - MySQL: mysqlclient 는 INSERT 의 executemany 를 다중 행 `INSERT … VALUES (…), (…)`로 바꿔 보내고, UPDATE 의 executemany 는 행마다 문장 하나를 보냅니다(`CaptureQueriesContext`에는 1건). rowcount 는 Django 가 켠 `CLIENT.FOUND_ROWS` 덕에 바뀐 행이 아니라 맞은 행을 셉니다. `bulk_insert(returning=...)`는 MySQL 에 `RETURNING`이 없어 실행 전 `NotSupportedError`이고, MariaDB 에서는 다중 `VALUES … RETURNING`으로 돕니다. `UPDATE … JOIN (VALUES ROW(…))`로 한 문장을 만드는 방식은 MariaDB 에 `VALUES ROW`가 없고 행 값의 타입을 정할 방법이 없어 쓰지 않았습니다. `INSERT … ON DUPLICATE KEY UPDATE`는 없는 키를 새 행으로 넣어 의미가 다릅니다.
  - Oracle: `bulk_insert(returning=...)`는 실행 전 `NotSupportedError`입니다(아래 "제약"의 `RETURNING`). 너무 긴 값은 ORM 쓰기와 같이 `DataError`가 아니라 `DatabaseError`(`ORA-12899`)입니다.
- 값 변환은 `execute`와 같습니다. 쓰는 행 값마다 `field.get_db_prep_save()`, `bulk_update`의 `key` 값은 ORM 조회처럼(`None`은 SQL `NULL`), `returning` 컬럼은 ORM 변환기를 거칩니다. 파라미터는 행마다 SQLAlchemy로 만들지 않고 컬럼마다 리스트 하나로 바로 만듭니다.
- **`key`·`columns`는 실제 컬럼 이름입니다.** `key`를 생략하면 테이블의 PK 컬럼이고, 줄 때는 ORM 별칭 `"pk"`가 아니라 컬럼 이름(`"id"`, FK 는 `"author_id"`, 복합 키는 `("author_id", "title")` 같은 튜플)입니다. `key="pk"`는 그런 컬럼이 없으면 실행 전 `ValueError`입니다.
- **빠진 값은 오류입니다.** 실행 전에 `ValueError`를 냅니다.
  - `bulk_update`: 키나 설정할 컬럼이 없는 행, 키가 같은 두 행(PostgreSQL은 그중 아무 행 하나로, executemany 는 둘 다 차례로 갱신해 결과가 달라지므로 거부). 중복 키가 있으면 아무것도 실행하지 않으므로 rowcount 를 돌려주지 않습니다. `columns`를 생략하면(`None`) 첫 행의 키 외 컬럼을 설정하고, 모든 행이 첫 행과 같은 컬럼 이름 집합을 가져야 합니다(컬럼이 빠지거나 더 있는 행은 실행 전 `ValueError`). `columns`를 주면 행의 다른 키는 무시합니다. 없는 키 값은 아무 행도 바꾸지 않고, rowcount 는 맞은 행만 셉니다(존재하지 않는 키는 세지 않음).
  - `bulk_insert`: 행끼리 컬럼 이름이 다르거나, 테이블 컬럼을 빠뜨린 경우. 실행 전 중복 키 검사는 `bulk_update`에만 있습니다. `bulk_insert`의 유일 키 중복(행끼리든 기존 행과든)은 ORM `bulk_create`처럼 DB 가 실행 중에 `IntegrityError`로 거부합니다. 자동 증가 PK 와 `db_default`가 있는 컬럼만 빠뜨릴 수 있습니다. Django 필드 `default`·`auto_now`를 채우지 않으므로 nullable 컬럼도 `None`을 직접 넣어야 합니다(잊은 컬럼이 조용히 `NULL`이 되지 않게).
  - 모든 컬럼을 기본값에 맡긴 행(`bulk_insert(Model, [{}, {}])`)도 행 수만큼 넣습니다. Django `bulk_create([Model(), Model()])`가 필드 값이 없는 객체에 쓰는 SQL 과 같은 꼴로, 자동 증가 PK 에 `NULL`(PostgreSQL 은 `DEFAULT`)을 주는 다중 행 INSERT 입니다(`INSERT INTO t (id) VALUES (NULL), (NULL)`, Oracle 은 `SELECT … UNION ALL`). `returning`·`batch_size`·원자성은 위와 같고, 한 문장의 행 수는 `bulk_batch_size()`를 넘지 않습니다. 자동 증가 PK 가 없으면(모든 컬럼이 `db_default`) PostgreSQL·MySQL 은 첫 컬럼에 `DEFAULT`를 주고, `VALUES`에 `DEFAULT`를 쓸 수 없는 SQLite·Oracle 은 실행 전 `NotSupportedError`입니다. `method="copy"`는 넣을 컬럼이 없어 실행 전 `ValueError`입니다.
- **`batch_size`**: 양의 정수면 그 행 수마다 문장 하나(SQLite 는 `executemany()` 호출 하나)로 나눕니다. `None`(기본값)이면 위와 같습니다. 0 이하나 정수가 아니면 실행 전에 `ValueError`입니다. SQLite 의 `returning` 배치는 `bulk_batch_size()`를 넘지 않습니다. 행 검사(누락, `bulk_update`의 키 중복)는 나누기 전에 전체 행으로 하고, rowcount 는 배치의 합, `returning` 결과는 배치와 상관없이 `rows` 순서입니다.
- **원자성**: 문장을 둘 이상 실행하는 호출(배치가 여럿이거나 SQLite `executemany()`)은 Django `bulk_create`·`bulk_update`처럼 `transaction.atomic(using=..., savepoint=False)` 안에서 돕니다. 한 배치가 실패하면 그 호출이 쓴 행은 모두 롤백됩니다. 바깥 `atomic()` 안에서는 savepoint 를 만들지 않으므로, 실패를 잡아 트랜잭션을 이어가려면 호출을 `atomic()`으로 한 번 더 감싸세요(ORM 쓰기와 같습니다).
- **언제 `batch_size`를 주나.** PostgreSQL 의 한 문장은 행 수에 비례해 클라이언트(파라미터 배열)와 서버 백엔드(배열과 `unnest` 결과) 메모리를 씁니다. 1,000,000행(컬럼 6개)에서 `bulk_insert`는 클라이언트 tracemalloc 피크 849 MiB, 서버 백엔드 233 MiB 였고, `batch_size=10_000`이면 226 MiB·6 MiB 로 줄면서 시간은 3.5% 빨랐습니다. `bulk_update`는 나누면 느려집니다(1,000,000행에서 `10_000`은 21% 느림, `200_000`은 3% 느림). 한 문장은 대상 테이블과 해시 조인 한 번으로, 작은 배치는 행마다 PK 인덱스 조회로 갱신하기 때문입니다(로컬 PostgreSQL 18 의 `EXPLAIN ANALYZE`로 확인). 그래서 기본값은 `None`이고, 수십만 행 이상에서 메모리가 문제일 때 `bulk_insert`는 `10_000`~`50_000`, `bulk_update`는 `200_000` 안팎을 권합니다. SQLite 는 차이가 작습니다(아래 "성능과 메모리" 7).
- **`method="copy"`(PostgreSQL + psycopg 3)**: `INSERT` 대신 `COPY t (컬럼…) FROM STDIN`(text 포맷)으로 넣습니다. Django 커서 아래의 psycopg 커서(`cursor.cursor.copy()`)로 실행하므로 Django 연결·트랜잭션 안에서 돕니다. 행 검사와 값 변환(`get_db_prep_save()`)은 `"insert"`와 같고, 탭·개행·백슬래시·`\N` 문자열의 이스케이프는 psycopg 가 맡습니다(테스트로 고정). `batch_size`를 주면 배치마다 `COPY` 하나, 둘 이상이면 같은 `atomic(savepoint=False)`입니다. 트리거·제약·FK 검사는 INSERT 와 같고, 제약 위반은 Django 예외(`IntegrityError`·`DataError`)로 올라옵니다. 반환값은 psycopg 가 COPY 뒤 `cursor.rowcount`로 주는 행 수입니다. **언제 쓰나**: `RETURNING`이 필요 없는 수십만 행 이상의 INSERT. 200,000행(컬럼 6개)에서 `"insert"`(unnest) 3,571 ms 대 1,925 ms 였고, 1,000,000행에서는 시간이 47% 줄고 클라이언트 tracemalloc 피크가 849 MiB → 193 MiB, 서버 백엔드 메모리 증가가 233 MiB → 0.5 MiB 였습니다(아래 "성능과 메모리" 2·7). COPY 는 행을 흘려보내므로 서버 메모리가 행 수를 따라 늘지 않습니다. `batch_size`는 클라이언트 메모리만 조금 줄입니다(1,000,000행에서 `10_000`이면 148 MiB, 시간은 4% 늘어남).
  - `returning`과 함께 쓰면 실행 전 `ValueError`입니다(COPY 에는 `RETURNING`·`ON CONFLICT`가 없음). PostgreSQL 이 아닌 연결이나 psycopg2 에서는 실행 전 `NotSupportedError`입니다. 조용히 `"insert"`로 바꾸지 않습니다. binary 포맷은 쓰지 않습니다(psycopg 의 binary COPY 는 컬럼마다 `set_types()`로 서버 타입을 정해야 하고, text 포맷은 서버가 INSERT 파라미터처럼 텍스트로 받아 컬럼 타입으로 읽습니다).
  - 쿼리 기록: `COPY`는 Django `CursorWrapper.execute()`를 지나지 않습니다. 브리지가 디버그 기록이 켜져 있을 때(`connection.queries_logged`) `COPY … FROM STDIN` 문장을 배치마다 `connection.queries`에 한 건씩 남기므로 `CaptureQueriesContext`·`assertNumQueries`에 잡히지만, **`connection.execute_wrapper()`에는 보이지 않습니다.**
- 빈 `rows`는 쿼리를 실행하지 않고 `0`(또는 `returning`이면 `[]`)을 돌려줍니다.
- **`RETURNING` 순서**: 결과는 `rows` 순서입니다(테스트로 고정). 다만 PostgreSQL·SQLite 모두 `RETURNING` 순서를 문서로 보장하지는 않으며, Django `bulk_create`가 PK 를 객체에 채울 때도 같은 동작에 기댑니다. 아래 "제약"을 보세요.
- signal·`save()`를 거치지 않는 것은 다른 브리지 쓰기와 같습니다(아래).

> ⚠️ **브리지로 쓴 내용은 모델 계층을 거치지 않습니다.** `save()`·`delete()`가 불리지 않고, `pre_save`/`post_save`/`pre_delete`/`post_delete` 등 **signal이 발생하지 않으며**, Django 필드의 `default`·`auto_now`도 채워지지 않습니다. 값은 모두 직접 넣어야 합니다.

## 다중 DB

`table_for()`의 테이블은 모델에서 오므로 어느 DB 에도 묶이지 않습니다. 어느 DB 에서 실행할지는 호출마다 `using`(Django DB 별칭)이 정하고, 같은 테이블·같은 문장을 `default`와 다른 DB 에 번갈아 실행해도 됩니다. dialect 는 실행할 때 `connections[using]`의 vendor 로 고르고, 컴파일 캐시는 vendor 별, 결과 컨버터 캐시는 연결 객체별이라 서로 섞이지 않습니다. `execute`·`execute_result`·`stream`·`raw`·`execute_pipeline`·`copy_to`·`bulk_*` 모두 `using`을 받습니다.

<!-- example:multi-db-using -->
```python
price_changes = table_for(PriceChange)  # 모델에서 만든 테이블은 어느 DB 에도 묶이지 않는다

stmt = sa.select(price_changes.c.sku, price_changes.c.price)
here = execute(stmt)  # default: [("SHIRT", Decimal("27.50"))]
there = execute(stmt, using="other")  # 같은 문장을 other 에서: [("TEA", Decimal("8.50"))]
```
<!-- /example:multi-db-using -->

**서로 다른 DB 의 테이블은 한 문장에서 JOIN 할 수 없습니다.** JOIN 은 한 연결 위에서 DB 서버가 실행하기 때문이고, Django ORM 도 SQLAlchemy 도 같습니다. 길은 둘입니다.

- **서버 링크.** PostgreSQL `postgres_fdw`·`dblink`, SQLite `ATTACH`, MySQL `FEDERATED`, Oracle DB link 처럼 한 DB 가 다른 DB 의 테이블을 자기 이름으로 보여 주면, 그 테이블은 한 연결 안의 이름입니다. 모델이 없으니 `sa.table()`로 손수 만들어 평범한 JOIN 을 그 연결(`using`)로 보냅니다. 손수 만든 테이블의 컬럼에는 Django 필드가 없으므로 그 값은 변환 없이 드라이버 값 그대로 오갑니다(위 "실행하기"의 컬럼으로 추적되지 않는 값과 같음). 아래는 SQLite 에서 실제로 도는 예입니다(`linked`는 다른 SQLite 파일).

<!-- example:multi-db-attach -->
```python
# 다른 SQLite 파일을 이 연결의 linked 스키마로 붙인다(연결 상태라 연결마다 한 번)
with connection.cursor() as cursor:
    cursor.execute("ATTACH DATABASE %s AS linked", [path])

products = table_for(Product)
remote_sales = sa.table("remote_sale", sa.column("product_id"), sa.column("quantity"), schema="linked")

stmt = (
    sa.select(products.c.sku, sa.func.sum(remote_sales.c.quantity))
    .join(remote_sales, remote_sales.c.product_id == products.c.id)
    .group_by(products.c.sku)
    .order_by(products.c.sku)
)
rows = execute(stmt)  # 한 연결 안의 평범한 JOIN: [("SHIRT", 3), ("TEA", 5)]
```
<!-- /example:multi-db-attach -->

- **애플리케이션에서 합치기.** 서버 링크가 없으면 Django 의 교차 DB `prefetch_related`처럼 한쪽에서 키를 뽑아 다른 쪽을 `in_()`으로 조회하고 파이썬에서 합칩니다. 문장이 둘이고 트랜잭션도 DB 마다 따로입니다.

<!-- example:multi-db-in -->
```python
products = table_for(Product)  # default DB 의 상품
price_changes = table_for(PriceChange)  # other DB 의 가격표

skus = [sku for (sku,) in execute(sa.select(products.c.sku).order_by(products.c.sku))]
new_prices = dict(
    execute(
        sa.select(price_changes.c.sku, price_changes.c.price).where(price_changes.c.sku.in_(skus)),
        using="other",
    )
)
rows = [(sku, new_prices.get(sku)) for sku in skus]
# [("JEANS", None), ("SHIRT", Decimal("27.50")), ("TEA", Decimal("8.50"))]
```
<!-- /example:multi-db-in -->

`arun`에는 `using`이 없습니다. `fn` 안의 호출이 저마다 `using`을 받습니다(아래 "비동기").

## 제약

브리지가 실행 전에 막는 것과, 막지 않거나 막을 수 없는 것들입니다.

- **RETURNING 은 그 DB 가 지원할 때만.** dialect 는 DB 에 연결하지 않고 컴파일하므로 어떤 DB 에도 `RETURNING`을 씁니다. 그래서 `RETURNING`이 있는 SQLAlchemy DML(`execute`·`execute_result`·`stream`, `bulk_insert(returning=...)`)은 문장 종류별로 실행 연결을 보고 **실행 전에** `django.db.NotSupportedError`를 냅니다.
  - SQLite: 3.35 부터 INSERT·UPDATE·DELETE 모두 지원합니다(Django 5.2 는 3.31 부터 받음). Django feature `can_return_columns_from_insert`가 거짓이면 셋 다 거부합니다.
  - MySQL: `RETURNING`이 없어 모두 거부합니다.
  - MariaDB(Django vendor 는 `"mysql"`): `INSERT … RETURNING`·`DELETE … RETURNING`은 실행하고 `UPDATE … RETURNING`만 거부합니다.
  - Oracle: 모두 거부합니다. Oracle 은 `RETURNING … INTO :출력변수`로 행을 출력 바인드에 넣는데 Django 커서로는 그 값을 받을 수 없습니다. 거부하지 않으면 에러 없이 행 대신 rowcount 가 돌아옵니다(2026-10-26, Oracle 23ai Free 에서 확인). Django 는 Oracle 에서 `can_return_columns_from_insert`를 참으로 알리지만 브리지는 vendor 로 거부합니다.
  - `sa.text("... RETURNING ...")`는 안을 볼 수 없어 검사하지 않고 DB 가 판단합니다.
- **SQLAlchemy Python 측 컬럼 `default`/`onupdate`는 적용할 수 없어 실행 전에 막습니다.** 직접 만든 `sa.Table`의 `Column(default=..., onupdate=...)`에 준 Python 값·callable 은 SQLAlchemy 실행 컨텍스트(`Engine`·`Connection`)가 채우는데 브리지는 Django 커서로 실행합니다. 컴파일러는 그 컬럼을 SQL 에 넣어 두므로(`INSERT ... (name, ...) VALUES (%s, ...)`, `UPDATE ... SET name=%s, ...`) 그대로 두면 `NULL`이 바인딩되어, nullable 컬럼에는 조용히 `NULL`이 저장되고 `onupdate` 컬럼은 UPDATE 마다 기존 값이 `NULL`로 덮입니다(SQLite·PostgreSQL 에서 확인). 그래서 그런 컬럼을 비워 둔 INSERT·UPDATE 는 `execute`·`execute_result`·`stream`·executemany 모두 **실행 전에** `ValueError`(`... Python-side default or onupdate of 테이블.컬럼 cannot be applied ...`)를 냅니다. 그 컬럼 값을 `.values()`나 params 로 직접 주면 그대로 실행합니다. `default=sa.func.now()`처럼 SQL 식인 default/onupdate 는 SQL 에 들어가므로 막지 않고 DB 가 계산합니다. DB 쪽 `server_default`·Django `db_default`도 DB 가 채우므로 영향이 없습니다. `table_for()` 테이블에는 이런 default 가 없고(Django 필드 `default`·`auto_now`도 채우지 않습니다), `bulk_*`는 `table_for()` 테이블만 받습니다.
- **PostgreSQL `standard_conforming_strings=off` 서버는 지원하지 않습니다.** 브리지는 연결하지 않고 컴파일하므로 리터럴 백슬래시를 "on"(PostgreSQL 기본값, Django 가 지원하는 모든 버전의 기본)으로 렌더합니다. "off" 서버에서는 컴파일러가 직접 쓴 리터럴(`sa.literal(..., literal_execute=True)` 등)의 백슬래시가 이스케이프로 읽혀 `'a\tb'`가 탭 문자가 됩니다(세션에서 "off"로 바꿔 PostgreSQL 18 에서 확인). 바인딩 값에는 영향이 없습니다.
- **MySQL 의 리터럴 백슬래시는 연결의 `sql_mode`를 따릅니다.** 컴파일러가 직접 쓴 리터럴(`sa.literal(..., literal_execute=True)` 등)의 백슬래시를 기본 `sql_mode`에서는 두 번 쓰고, `NO_BACKSLASH_ESCAPES`면 한 번 씁니다. Django 가 연결마다 한 번 읽는 `connection.sql_mode`를 보므로, 연결을 연 뒤 세션에서 `SET sql_mode`로 바꾸면 따라가지 않습니다.
- **Oracle 의 식별자는 Django 식으로 씁니다.** Django 의 Oracle `quote_name()`은 모든 이름을 대문자로 바꾸고 30자를 넘으면 해시로 줄입니다(`a_column_name_longer_than_thirty_characters` → `A_COLUMN_NAME_LONGER_THAN_9E63`). SQLAlchemy 의 Oracle dialect 는 소문자 이름을 따옴표 없이 쓰지만(Oracle 이 대문자로 읽어 같음) 대소문자가 섞인 이름(`db_column="MixedCase"`)·긴 이름·예약어(`uid`, `level`)는 그대로 따옴표로 감싸 Django 가 만든 적 없는 컬럼을 가리킵니다(`ORA-00904`). 그래서 브리지의 Oracle dialect 는 테이블·컬럼·별칭을 모두 Django 처럼 대문자로 줄여 따옴표로 씁니다. `sa.sql.quoted_name("Legacy", quote=True)`처럼 대소문자를 지키라고 한 이름도 대문자로 씁니다. Django 의 `quote_name()`이 따옴표로 감싼 이름까지 대문자로 바꾸므로 Django 가 관리하는 스키마에는 대소문자를 구분하는 이름이 없고, 브리지는 Django 와 같은 이름을 쓰는 쪽을 택했습니다. Django 밖에서 대소문자를 구분해 만든 테이블은 `sa.text()`로 쓰세요.
- **Oracle 의 그 밖의 차이.** 브리지는 SQL 의미를 그대로 쓰므로 ORM 이 Oracle 에서 바꿔 주는 것을 바꾸지 않습니다.
  - 빈 문자열은 `NULL`입니다. `col == ""`은 아무 행에도 맞지 않습니다(ORM 은 `filter(col="")`을 `IS NULL`로 바꿈). `col.is_(None)`을 쓰세요. 읽을 때는 ORM 과 브리지 모두 `""`를 줍니다.
  - `TextField`는 `NCLOB`이라 `==`로 비교하면 `ORA-22848`입니다(ORM 은 `DBMS_LOB.SUBSTR()`로 감쌈).
  - 다중 행 `VALUES`(`insert(t).values([{...}, {...}])`)는 SQLAlchemy 가 연결하지 않은 Oracle dialect 에서 컴파일하지 않아 실행 전 `CompileError`입니다. params 리스트(executemany)나 `bulk_insert`를 쓰세요.
  - 컬럼이 없는 식이나 `sa.text()`로 고른 `NCLOB`·`BLOB` 값은 oracledb 의 LOB 객체로 옵니다(`.read()`). 컬럼을 고르면 ORM 변환기가 `str`·`bytes`로 바꿉니다.
  - `sum()`·`avg()`처럼 정밀도가 없는 숫자를 Django 의 Oracle 커서는 텍스트에 소수점이 있는지로 `int`나 `Decimal`로 줍니다. 브리지는 ORM 처럼(`BaseExpression.convert_value`) 컬럼 필드의 타입으로 맞춥니다(`DecimalField`의 합은 `Decimal`).
  - `SELECT` 목록의 불리언 식(`(price > 2).label("gt")`, `sa.exists()`)은 Oracle 23ai 부터 됩니다. 19c 는 확인하지 않았습니다(아래 "지원 대상").
- **`bulk_insert(method="copy")`는 `execute_wrapper()`에 보이지 않습니다.** psycopg 의 `Cursor.copy()`는 Django 커서의 `execute()`를 거치지 않기 때문입니다. `connection.queries`에는 배치마다 한 건씩 남깁니다(위 "대량 쓰기"). PostgreSQL + psycopg 3 전용이고, 다른 연결에서는 실행 전 `NotSupportedError`입니다.
- **`copy_to`는 PostgreSQL + psycopg 3 전용이고 `execute_wrapper()`에 보이지 않습니다.** 값은 Django 변환이 아닌 서버의 텍스트 표현이고, 트랜잭션 안에서 일찍 `close()`하면 남은 데이터를 다 받습니다(위 "CSV 로 내보내기").
- **`bulk_insert(returning=...)`의 행 순서는 DB 가 문서로 보장하지 않습니다.** PostgreSQL·SQLite 모두 지금은 입력 순서로 돌려주고 Django `bulk_create`도 같은 가정에 기대지만, 순서가 중요하면 행을 식별할 키(자연 키, `uid` 등)를 함께 `RETURNING`해서 그 값으로 짝지으세요. 예: `bulk_insert(books, rows, returning=["uid", "id"])`.

## 비동기

Django의 async ORM(`aget()` 등)은 내부적으로 쿼리마다 `sync_to_async`로 스레드에 넘기는 구조이고, Django 문서도 "async 모드에서는 아직 트랜잭션이 동작하지 않는다"고 적고 있습니다. 브리지는 작업 단위 전체를 스레드에 **한 번** 넘기는 `arun` 하나만 제공합니다. 얻는 것은 스레드 위임 횟수가 줄고 async 코드에서도 트랜잭션을 쓸 수 있는 것이지, 진짜 비동기 I/O는 아닙니다.

### `arun`

```python
import sqlalchemy as sa
from django.db import transaction
from django_sqlalchemy_bridge import arun, execute, table_for

from shop.models import Order

orders = table_for(Order)


def ship(order_ids):  # 동기 함수: 쿼리 여러 개와 atomic()을 담은 작업 단위
    with transaction.atomic():
        n = execute(sa.update(orders).where(orders.c.id.in_(order_ids)).values(status="shipped"))
        transaction.on_commit(notify_shipping)
        return n


async def view(request):
    n = await arun(ship, [1, 2, 3])
```

- `arun(fn, /, *args, **kwargs)`는 `fn(*args, **kwargs)`를 `sync_to_async(thread_sensitive=True)`로 한 번에 실행하고 반환값을 돌려줍니다. `fn`이 낸 예외도 그대로 올라옵니다. `fn` 안에서는 `execute`·`stream`·`bulk_*`·`atomic()`·`on_commit`·ORM을 동기 코드처럼 씁니다.
- `fn`은 위치 전용 인자라 `fn`이나 `using`이라는 키워드 인자도 그대로 `fn`에 넘어갑니다. `arun`에는 `using`이 없습니다. 어느 DB를 쓸지는 `fn` 안의 `execute(..., using=...)`·`atomic(using=...)`이 정합니다(위 "다중 DB").
- 코루틴 함수를 넘기면 `TypeError`입니다(`await fn()`을 쓰세요). 동기 함수가 awaitable을 돌려줘도 `TypeError`입니다.
- `arun` 여러 건을 `asyncio.gather`로 동시에 돌려도 서로 섞이지 않습니다. thread-sensitive 호출은 한 스레드에서 차례로 돌고, 각 `fn`의 트랜잭션은 그 `fn` 안에서 시작해 끝나므로 한쪽의 롤백이 다른 쪽의 커밋에 영향을 주지 않습니다(테스트로 확인).
- **연결 수명.** `fn` 앞뒤로 Django `close_old_connections()`와 같은 정리를 합니다. 그 스레드의 연결 중 오류 뒤 쓸 수 없게 된 것, `CONN_MAX_AGE`가 지난 것을 닫습니다(Channels `database_sync_to_async`와 같은 동작). 요청 밖에서 오래 도는 코루틴(워커, consumer)은 Django의 요청 시작·끝 정리를 받지 못해, 서버가 끊은 연결이 그 스레드에서 계속 `connection is closed`로 실패하기 때문입니다. 이 정리가 있으면 끊긴 뒤 첫 호출만 실패하고 다음 호출은 새 연결을 씁니다. `CONN_HEALTH_CHECKS: True`면 첫 호출부터 새 연결을 씁니다(PostgreSQL에서 테스트로 확인).
  - 그래서 `CONN_MAX_AGE: 0`(기본값)이면 `arun` 호출마다 연결을 새로 엽니다. 요청 하나에서 여러 번 부른다면 `CONN_MAX_AGE`를 주거나 PostgreSQL 연결 풀(`OPTIONS: {"pool": True}`)을 쓰세요. 측정은 아래 "성능과 메모리"의 5 를 보세요.
  - `atomic()` 블록 안에 있는 연결(예: 바깥 동기 코드가 연 트랜잭션, Django `TestCase`)은 닫지 않습니다.
- async 코드에서 `execute`·`stream`·`bulk_*`를 `arun` 없이 바로 부르면 ORM처럼 `SynchronousOnlyOperation`이 납니다. 이 안전장치는 우회하지 않습니다.

**Django Channels.** thread-sensitive 호출은 Django ASGI 핸들러 안에서는 요청마다 스레드 하나를 쓰지만, Channels consumer에는 그런 구분이 없어 모든 consumer의 `arun`(그리고 `database_sync_to_async`)이 프로세스 전체에서 **스레드 하나로 줄을 섭니다.** DB 작업이 긴 consumer가 많으면 서로를 기다립니다. 브리지는 Channels에 의존하지 않습니다.

### 넣지 않는 것

- **`async with atomic()` 형태는 넣지 않습니다.** `atomic()`의 진입·종료를 따로 `sync_to_async`로 넘기면, 같은 스레드를 쓰는 코루틴들이 **연결 하나와 그 트랜잭션 상태를 함께 씁니다.** 먼저 연 코루틴의 트랜잭션 안에서 다른 코루틴의 `atomic()`은 savepoint가 되고, 트랜잭션이 없는 쓰기는 그 트랜잭션에 그대로 섞입니다. 그래서 정상 종료한 코루틴의 쓰기가 다른 코루틴의 실패와 함께 **예외 없이 롤백됩니다.** 이렇게 흉내 낸 구현을 `gather`로 두 건 돌려 SQLite·PostgreSQL 모두에서 재현했습니다(먼저 연 쪽이 실패하든, 정상 쪽이 먼저 블록을 나오든 정상 쪽의 행이 사라짐).
- **SQLAlchemy 비동기 엔진(`AsyncEngine`)과 psycopg 3 비동기 연결 직접 사용은 채택하지 않습니다.** 둘 다 Django 밖에서 따로 연결을 열기 때문에 Django 트랜잭션·잠금·쿼리 집계(`execute_wrapper`, `assertNumQueries`) 밖에서 돕니다. "연결과 트랜잭션은 Django가 관리한다"는 원칙과 맞지 않습니다.

## 성능과 메모리

아래 표의 수치는 모두 저장소의 `bench/` 스크립트가 쓴 결과 파일(`bench/results/*.json`)에서 `bench/readme.py`가 만든 것입니다. 표마다 출처 파일을 적었고, 결과 파일에는 회차별 원값과 버전·DB 서버·머신 정보가 함께 들어 있습니다. 표가 결과 파일과 어긋나면 테스트(`tests/test_bench_readme.py`)가 실패합니다.

**GitHub Actions 의 `ubuntu-latest` 러너 한 대에서 잰 수치입니다**(`.github/workflows/bench.yml`, 실행 기록은 결과 파일의 `ci_run`). 러너는 다른 작업과 하드웨어를 나눠 쓰는 공유 VM 이라 같은 측정의 회차 사이에도 수십 % 차이가 나기도 하고(결과 파일의 `times_s`), 차이가 10% 안팎인 비교는 순서가 뒤바뀔 수 있습니다. PostgreSQL 은 같은 VM 의 서비스 컨테이너라 네트워크를 건너는 실제 서버 배치보다 왕복이 쌉니다. 절대값보다 같은 표 안의 상대 비교로 읽으세요.

<!-- bench:env-postgres -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · AMD EPYC 9V45 96-Core Processor (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres -->

<!-- bench:env-sqlite -->
SQLite 3.45.1 · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · AMD EPYC 9V45 96-Core Processor (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-sqlite -->

다시 재기:

```sh
uv sync --group bench
DSB_PG_HOST=127.0.0.1 DSB_PG_PORT=55432 DSB_PG_USER=dsb DSB_PG_PASSWORD=dsb DSB_PG_NAME=dsb \
  uv run --group bench python -m bench --db postgres   # 시나리오 지정: python -m bench update read ...
uv run --group bench python -m bench --db sqlite
uv run python -m bench.readme --write                  # 시나리오·DB 마다 날짜가 가장 늦은 결과 파일로
```

**측정 방법.**

- 시간은 측정 대상 호출 하나를 `perf_counter`로 잰 값이고, 예열 1회 뒤 5회 중 **최솟값**입니다(원값 5개는 결과 파일에). 메모리는 따로 한 번 더 돌려 `tracemalloc` 피크(그 호출이 새로 할당한 양)를 잽니다. 대량 읽기는 새 프로세스에서 결과를 쥔 채 RSS 증가분도 잽니다(3회 중앙값).
- 매 회차 같은 데이터·같은 상태에서 시작합니다. 쓰기는 트랜잭션 안에서 재고 롤백하며, PostgreSQL 은 회차마다 `VACUUM ANALYZE`를 합니다.
- **입력 만들기는 시간에 넣지 않습니다.** Django·django-pgbulk 는 모델 인스턴스, 브리지·SQLAlchemy 는 딕셔너리를 미리 만들어 두고 쓰기 호출만 잽니다. 실제로는 인스턴스를 만드는 비용이 Django 쪽에 더 붙습니다.
- 비교 대상은 그 도구다운 방법으로 씁니다. Django `bulk_update`·`bulk_create`는 `batch_size` 기본값과 `1000` 둘 다, django-pgbulk 는 `update()`·`copy()`(text·binary)입니다.
- **"별도 엔진"이라 적은 SQLAlchemy 수치는 브리지가 아닙니다.** SQLAlchemy 를 단독으로 쓸 때의 기준선으로, 자체 `Engine`이 Django 밖에서 따로 연결을 엽니다(Django 트랜잭션·쿼리 집계 밖). 브리지는 이 방식을 쓰지 않습니다(위 "설계 원칙").

### 1. PK 기준 대량 UPDATE (컬럼 3개)

<!-- bench:pg-update -->
| | 5,000행 시간 (ms) | 50,000행 시간 (ms) |
|---|---:|---:|
| Django `bulk_update()` (batch_size 기본값) | 4,156 | 측정 안 함 ¹ |
| Django `bulk_update(batch_size=1000)` | 1,484 | 15,569 |
| django-pgbulk `update()` | 59.8 | 1,130 |
| SQLAlchemy ORM bulk UPDATE (별도 엔진) | 67.1 | 934 |
| 브리지 `execute(stmt, [params...])` (executemany) | 110 | 1,184 |
| 브리지 `bulk_update()` | 32.2 | 333 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-update.json`](bench/results/2026-10-25-postgres-update.json)</sub>
<!-- /bench:pg-update -->

¹ PostgreSQL 에서 `batch_size`가 없으면 Django 는 모든 행을 컬럼마다 `CASE WHEN id = … THEN …` 하나에 담은 UPDATE 한 문장을 보냅니다. 5,000행에서도 위처럼 느리고, 50,000행에서는 (이전 로컬 측정 때) 서버 백엔드가 CPU 100%·메모리 3.6 GB 로 30분 넘게 끝나지 않고 취소에도 응답하지 않아 프로세스를 강제로 끝냈습니다. 그래서 재지 않았습니다.

² SQLite 의 `batch_size` 기본값 실패는 Django 6.1.2 에서 재현됩니다. 기본 배치 크기를 `SQLITE_LIMIT_VARIABLE_NUMBER`를 행당 파라미터 `2 + 컬럼 수`로 나눠 정하는데, 실제 문장은 행당 `2 × 컬럼 수 + 1`개를 써서 두 컬럼 이상을 갱신하면 한도를 넘습니다(같은 5,000행·3컬럼이 Django 5.2.18 에서는 성공). 언제 넘는지는 SQLite 빌드의 한도에 달렸습니다. 이 측정의 Ubuntu SQLite 는 한도가 250,000 이라 기본 배치가 50,000행이 되고, 5,000행은 한 문장(파라미터 35,001개)으로 성공하지만 50,000행(350,001개)은 실패합니다. 한도가 SQLite 기본값 32,766 인 빌드(예: uv 가 설치하는 Python)에서는 5,000행부터 실패합니다.

<!-- bench:sqlite-update -->
| | 5,000행 시간 (ms) | 50,000행 시간 (ms) |
|---|---:|---:|
| Django `bulk_update()` (batch_size 기본값) | 1,385 | 실패: `OperationalError` ² |
| Django `bulk_update(batch_size=1000)` | 1,060 | 14,651 |
| SQLAlchemy ORM bulk UPDATE (별도 엔진) | 21.1 | 292 |
| 브리지 `execute(stmt, [params...])` (executemany) | 28.9 | 321 |
| 브리지 `bulk_update()` | 11.7 | 137 |

<sub>결과 파일: [`bench/results/2026-10-25-sqlite-update.json`](bench/results/2026-10-25-sqlite-update.json)</sub>
<!-- /bench:sqlite-update -->

### 2. 대량 INSERT

PostgreSQL 표는 위 환경과 다른 실행(다른 러너 CPU)에서 쟀습니다.

<!-- bench:env-postgres-insert -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · AMD EPYC 7763 64-Core Processor (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres-insert -->

<!-- bench:pg-insert -->
| | 20,000행 시간 (ms) | 200,000행 시간 (ms) |
|---|---:|---:|
| Django `bulk_create()` (batch_size 기본값) | 440 | 4,813 |
| Django `bulk_create(batch_size=1000)` | 460 | 4,829 |
| django-pgbulk `copy()` | 231 | 2,297 |
| django-pgbulk `copy(binary=True)` | 210 | 2,069 |
| SQLAlchemy Core `insert()` (별도 엔진) | 449 | 5,045 |
| SQLAlchemy Core `insert().returning(id)` (별도 엔진) | 702 | 7,627 |
| 브리지 `execute(insert(t), [rows...])` (executemany) | 1,367 | 12,171 |
| 브리지 `bulk_insert()` | 327 | 3,571 |
| 브리지 `bulk_insert(returning=['id'])` | 342 | 3,732 |
| 브리지 `bulk_insert(method="copy")` | 174 | 1,925 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-insert.json`](bench/results/2026-10-26-postgres-insert.json)</sub>
<!-- /bench:pg-insert -->

<!-- bench:sqlite-insert -->
| | 20,000행 시간 (ms) | 200,000행 시간 (ms) |
|---|---:|---:|
| Django `bulk_create()` (batch_size 기본값) | 316 | 10,066 |
| Django `bulk_create(batch_size=1000)` | 231 | 7,392 |
| SQLAlchemy Core `insert()` (별도 엔진) | 83.1 | 883 |
| SQLAlchemy Core `insert().returning(id)` (별도 엔진) | 93.6 | 1,261 |
| 브리지 `execute(insert(t), [rows...])` (executemany) | 205 | 2,152 |
| 브리지 `bulk_insert()` | 124 | 1,474 |
| 브리지 `bulk_insert(returning=['id'])` | 149 | 1,520 |

<sub>결과 파일: [`bench/results/2026-10-25-sqlite-insert.json`](bench/results/2026-10-25-sqlite-insert.json)</sub>
<!-- /bench:sqlite-insert -->

### 3. 20,000행 읽기 (컬럼 7개): 시간과 메모리

<!-- bench:pg-read -->
| | 시간 (ms) | tracemalloc 피크 (MiB) | RSS 증가 (MiB) |
|---|---:|---:|---:|
| Django 모델 객체 `list(qs)` | 71.7 | 14.2 | 15.8 |
| Django `values()` | 39.7 | 12.4 | 16.4 |
| Django `values_list()` | 33.0 | 7.2 | 11.2 |
| SQLAlchemy ORM 객체 (별도 엔진) | 100 | 25.2 | 29.7 |
| SQLAlchemy Core `Row` (별도 엔진) | 25.3 | 8.4 | 12.6 |
| 브리지 `execute_result().all()` (`Row`) | 36.3 | 8.4 | 9.0 |
| 브리지 `execute()` (튜플) | 32.1 | 7.2 | 11.3 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-read.json`](bench/results/2026-10-25-postgres-read.json)</sub>
<!-- /bench:pg-read -->

<!-- bench:sqlite-read -->
| | 시간 (ms) | tracemalloc 피크 (MiB) | RSS 증가 (MiB) |
|---|---:|---:|---:|
| Django 모델 객체 `list(qs)` | 120 | 15.6 | 15.3 |
| Django `values()` | 82.9 | 13.8 | 15.9 |
| Django `values_list()` | 76.9 | 10.5 | 12.6 |
| SQLAlchemy ORM 객체 (별도 엔진) | 84.6 | 25.4 | 28.1 |
| SQLAlchemy Core `Row` (별도 엔진) | 34.4 | 11.9 | 14.0 |
| 브리지 `execute_result().all()` (`Row`) | 73.3 | 10.4 | 12.7 |
| 브리지 `execute()` (튜플) | 73.9 | 10.4 | 12.4 |

<sub>결과 파일: [`bench/results/2026-10-25-sqlite-read.json`](bench/results/2026-10-25-sqlite-read.json)</sub>
<!-- /bench:sqlite-read -->

결과를 쥐지 않고 쪼개 읽기만 할 때(PostgreSQL, 서버 측 커서):

<!-- bench:pg-stream -->
| | 시간 (ms) | tracemalloc 피크 (MiB) |
|---|---:|---:|
| Django `values_list().iterator(chunk_size=2000)` | 34.6 | 1.4 |
| 브리지 `stream(chunk_size=2000)` | 32.4 | 1.4 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-read.json`](bench/results/2026-10-25-postgres-read.json)</sub>
<!-- /bench:pg-stream -->

**메모리 지침.** 행을 많이 읽을 때는 모델 객체(Django 든 SQLAlchemy ORM 이든)가 아니라 튜플·행으로 받으세요. 브리지 `execute()`의 튜플은 Django `values_list()`와 같은 수준이고, 다 쥘 필요가 없으면 `stream()`이 메모리를 청크 크기로 묶습니다(Django `iterator()`와 같은 수준).

### 4. 쿼리 생성 오버헤드: PK 조회 5,000번

<!-- bench:pg-lookup -->
| | 시간 (ms) | 호출당 (µs) |
|---|---:|---:|
| Django 커서에 SQL 직접 | 877 | 175 |
| Django `objects.get(pk=...)` | 1,844 | 369 |
| Django `values_list(...).get(pk=...)` | 2,045 | 409 |
| SQLAlchemy Core, 문장 1회 생성 + `bindparam` (별도 엔진) | 902 | 180 |
| SQLAlchemy Core, 매번 문장 생성 (별도 엔진) | 1,300 | 260 |
| 브리지 `execute()`, 문장 1회 생성 + `bindparam` | 1,188 | 238 |
| 브리지 `execute()`, 매번 문장 생성 | 2,253 | 451 |
| (참고) 브리지 내부 `prepare()`만 (캐시 적중 + 파라미터 변환), DB 없음 | 33.2 | 6.65 |
| (참고) 내부 `prepare()` 1회 후 커서 하나로 실행·변환만 | 878 | 176 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-lookup.json`](bench/results/2026-10-26-postgres-lookup.json)</sub>
<!-- /bench:pg-lookup -->

<!-- bench:sqlite-lookup -->
| | 시간 (ms) | 호출당 (µs) |
|---|---:|---:|
| Django 커서에 SQL 직접 | 41.9 | 8.37 |
| Django `objects.get(pk=...)` | 672 | 134 |
| Django `values_list(...).get(pk=...)` | 837 | 167 |
| SQLAlchemy Core, 문장 1회 생성 + `bindparam` (별도 엔진) | 119 | 23.8 |
| SQLAlchemy Core, 매번 문장 생성 (별도 엔진) | 322 | 64.4 |
| 브리지 `execute()`, 문장 1회 생성 + `bindparam` | 127 | 25.3 |
| 브리지 `execute()`, 매번 문장 생성 | 895 | 179 |
| (참고) 브리지 내부 `prepare()`만 (캐시 적중 + 파라미터 변환), DB 없음 | 33.0 | 6.60 |
| (참고) 내부 `prepare()` 1회 후 커서 하나로 실행·변환만 | 67.2 | 13.4 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-lookup.json`](bench/results/2026-10-26-sqlite-lookup.json)</sub>
<!-- /bench:sqlite-lookup -->

- 이 표는 위 환경 줄과 다른 실행(결과 파일의 `ci_run`, 러너 CPU AMD EPYC 9V74)에서 다시 잰 값입니다(#28 컴파일 캐시, #60 결과 변환기 캐시 반영). 러너 CPU 가 달라 절대값을 위 표들과 견주지 마세요.
- 브리지는 **같은 문장 객체를 다시 실행하면 컴파일을 건너뜁니다**(위 "실행하기"의 컴파일 캐시). `bindparam`으로 한 번 만든 문장은 첫 호출에만 컴파일하고, 매번 새로 만든 문장은 매번 컴파일합니다. 두 행의 차이(PostgreSQL 약 210µs, SQLite 약 150µs)가 호출당 컴파일 비용입니다. 문장을 한 번 만들어 `bindparam`으로 재사용하세요.
- "(참고) `prepare()`만" 행은 캐시 적중 뒤 남는 브리지 쪽 일(파라미터 변환, 연결 객체에 둔 결과 변환기 꺼내기)이고 호출당 약 6.6µs 입니다. "(참고) `prepare()` 1회" 행은 그것마저 한 번만 하고 커서 하나로 실행·변환만 한 값으로, 공개 API 가 아닙니다. 브리지 `execute()`는 호출마다 Django 커서를 새로 엽니다(Django `objects.get()`도 마찬가지). PostgreSQL 에서 `execute()`가 두 참고 행의 합보다 느린 몫은 그 때문으로 보이지만 따로 재지는 않았습니다.
- PostgreSQL 에서 `bindparam` 재사용 브리지(호출당 238µs)는 Django `objects.get()`(369µs)보다 빠르고, SQLAlchemy 단독 엔진(180µs)과의 차이는 58µs 입니다. SQLite 에서는 25.3µs 로 SQLAlchemy 단독(23.8µs)과 같은 자릿수이고 Django ORM 조회(134µs 이상)보다 5배 남짓 빠릅니다.

### 5. 비동기 (PostgreSQL, PK 조회)

<!-- bench:pg-async -->
| | 시간 (ms) | 쿼리당 (µs) |
|---|---:|---:|
| (기준) 동기 코드에서 `execute()` 1,000번 | 266 | 266 |
| `aget()` 1,000번 순차 | 403 | 403 |
| `arun()` 1,000번 순차 (호출마다 쿼리 1개) | 402 | 402 |
| 같은 것, `CONN_MAX_AGE=0` (호출마다 재연결) | 5,305 | 5,305 |
| `gather` 100건 × `aget()` | 32.4 | 324 |
| `gather` 100건 × `arun()` | 36.6 | 366 |
| `gather` 1,000건 × `aget()` | 377 | 377 |
| `gather` 1,000건 × `arun()` | 343 | 343 |
| 작업 단위(쿼리 5개) 200개 순차, 쿼리마다 `aget()` | 375 | 375 |
| 작업 단위(쿼리 5개) 200개 순차, 단위마다 `arun()` | 306 | 306 |
| 작업 단위 200개 `gather`, 쿼리마다 `aget()` | 311 | 311 |
| 작업 단위 200개 `gather`, 단위마다 `arun()` | 283 | 283 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-async.json`](bench/results/2026-10-25-postgres-async.json)</sub>
<!-- /bench:pg-async -->

- 쿼리 하나짜리 호출에서는 `arun`과 `aget()`이 같은 수준입니다. 둘 다 thread-sensitive 스레드 하나에서 차례로 돌기 때문에 `gather`로 동시에 불러도 처리량이 크게 늘지 않습니다(순차 대비 쿼리당 시간이 5~20% 줄어드는 정도).
- `arun`의 이점은 속도보다 **트랜잭션을 async 코드에서 쓸 수 있다는 것**입니다. 쿼리 여러 개를 한 번에 넘기면 스레드 위임이 줄어 순차 실행에서 빨라집니다(작업 단위 200개 순차에서 약 20%). 작업 단위를 `gather`로 동시에 돌린 경우는 차이가 10% 안팎이라 이 측정만으로 어느 쪽이 빠르다고 하기 어렵습니다.
- **`CONN_MAX_AGE=0`(기본값)이면 `arun`은 호출마다 다시 연결합니다**(위 "연결 수명"). 표에서 보듯 가장 큰 비용이므로 `arun`을 자주 부른다면 `CONN_MAX_AGE`나 연결 풀을 쓰세요.
- SQLAlchemy `AsyncEngine`·psycopg 비동기 연결은 재지 않았습니다. #1 에서 Django 트랜잭션·잠금·쿼리 집계 밖에서 돈다는 이유로 탈락했고 브리지 API 가 아닙니다.

### 6. CTE·윈도 함수 리포트 쿼리 (PostgreSQL, 20,000행)

<!-- bench:pg-report -->
| | CTE + `RANK()` (500행) 시간 (ms) |
|---|---:|
| Django 커서에 SQL 직접 | 3.38 |
| Django ORM (CTE 없이 집계 + `Window`) | 3.68 |
| Django + django-cte | 4.02 |
| 브리지 (`select().cte()` + `rank().over()`) | 3.71 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-report.json`](bench/results/2026-10-25-postgres-report.json)</sub>
<!-- /bench:pg-report -->

<!-- bench:pg-running -->
| | 누적 합 `SUM() OVER` (20,000행) 시간 (ms) |
|---|---:|
| Django ORM `Window(Sum(...), partition_by=...)` | 25.0 |
| 브리지 `func.sum().over(partition_by=...)` | 20.3 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-report.json`](bench/results/2026-10-25-postgres-report.json)</sub>
<!-- /bench:pg-running -->

모든 변형이 같은 행을 돌려주는지 측정 중에 확인합니다. 결과가 작은 집계(위 표)는 DB 실행이 시간의 대부분이라 도구 차이가 거의 없고, 행이 많은 결과(아래 표)는 행을 만들고 변환하는 비용에서 차이가 납니다.

### 7. 대량 쓰기의 `batch_size` (1,000,000행)

`batch_size`에 따른 시간, 클라이언트 tracemalloc 피크, (PostgreSQL) 서버 백엔드 메모리입니다. PostgreSQL 에서 `batch_size=None`은 한 문장이고, `method="copy"`는 배치마다 `COPY` 하나입니다. 서버 백엔드 메모리는 따로 한 번 더, 새 연결에서 그 연결의 PostgreSQL 백엔드 프로세스 `/proc/<pid>/status`를 2 ms 마다 읽어 호출 중 `RssAnon`(공유 버퍼를 뺀 개인 메모리) 최댓값에서 호출 전 값을 뺀 것입니다. 200,000행 결과와 `VmHWM`은 결과 파일에 있습니다. 이 절의 표는 위 표들과 다른 실행(다른 러너 CPU)에서 쟀습니다.

<!-- bench:env-postgres-batch -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · AMD EPYC 7763 64-Core Processor (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres-batch -->

<!-- bench:pg-batch-insert -->
| | 시간 (ms) | tracemalloc 피크 (MiB) | 서버 백엔드 메모리 증가 (MiB) |
|---|---:|---:|---:|
| `bulk_insert()` (batch_size=None) | 17,958 | 848.9 | 233.3 |
| `bulk_insert(batch_size=10_000)` | 17,333 | 226.0 | 6.1 |
| `bulk_insert(batch_size=50_000)` | 17,368 | 388.9 | 26.9 |
| `bulk_insert(batch_size=200_000)` | 17,582 | 643.5 | 68.2 |
| `bulk_insert(returning=['id'])` (batch_size=None) | 18,551 | 848.9 | 235.8 |
| `bulk_insert(returning=['id'], batch_size=50_000)` | 17,417 | 259.4 | 28.9 |
| `bulk_insert(method="copy")` (batch_size=None) | 9,549 | 193.3 | 0.5 |
| `bulk_insert(method="copy", batch_size=10_000)` | 9,929 | 148.0 | 0.5 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-batch.json`](bench/results/2026-10-26-postgres-batch.json)</sub>
<!-- /bench:pg-batch-insert -->

<!-- bench:pg-batch-update -->
| | 시간 (ms) | tracemalloc 피크 (MiB) | 서버 백엔드 메모리 증가 (MiB) |
|---|---:|---:|---:|
| `bulk_update()` (batch_size=None) | 16,173 | 609.6 | 196.5 |
| `bulk_update(batch_size=10_000)` | 19,544 | 163.1 | 12.8 |
| `bulk_update(batch_size=50_000)` | 20,546 | 299.0 | 26.8 |
| `bulk_update(batch_size=200_000)` | 16,713 | 435.9 | 51.5 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-batch.json`](bench/results/2026-10-26-postgres-batch.json)</sub>
<!-- /bench:pg-batch-update -->

SQLite 에서는 차이가 작습니다. 시간은 3% 안, tracemalloc 피크는 많아야 7% 줄었습니다. 어차피 `executemany()`가 행마다 실행하기 때문입니다(`returning`은 `bulk_batch_size()` 단위 배치).

<!-- bench:sqlite-batch-insert -->
| | 시간 (ms) | tracemalloc 피크 (MiB) |
|---|---:|---:|
| `bulk_insert()` (batch_size=None) | 12,687 | 221.4 |
| `bulk_insert(batch_size=10_000)` | 12,536 | 205.4 |
| `bulk_insert(batch_size=50_000)` | 12,599 | 206.0 |
| `bulk_insert(batch_size=200_000)` | 12,625 | 208.4 |
| `bulk_insert(returning=['id'])` (batch_size=None) | 12,702 | 304.6 |
| `bulk_insert(returning=['id'], batch_size=50_000)` | 12,849 | 304.6 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-batch.json`](bench/results/2026-10-26-sqlite-batch.json)</sub>
<!-- /bench:sqlite-batch-insert -->

<!-- bench:sqlite-batch-update -->
| | 시간 (ms) | tracemalloc 피크 (MiB) |
|---|---:|---:|
| `bulk_update()` (batch_size=None) | 4,876 | 148.2 |
| `bulk_update(batch_size=10_000)` | 4,760 | 148.2 |
| `bulk_update(batch_size=50_000)` | 4,841 | 148.2 |
| `bulk_update(batch_size=200_000)` | 4,863 | 148.2 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-batch.json`](bench/results/2026-10-26-sqlite-batch.json)</sub>
<!-- /bench:sqlite-batch-update -->

### 8. 여러 독립 문장: `execute()` 순차 대 `execute_pipeline()` (PostgreSQL)

같은 문장(`bindparam` 으로 1회 생성)으로 PK 조회를 하나씩 `execute()` 하는 것과 `(문장, params)` 항목으로 `execute_pipeline()` 한 번에 넘기는 것, 그리고 서로 독립인 집계 쿼리 5개(대시보드 한 화면)를 비교합니다. 모든 변형이 같은 행을 돌려주는지 확인합니다.

<!-- bench:pg-pipeline -->
| | PK 조회 1,000번 시간 (ms) | 5,000번 시간 (ms) |
|---|---:|---:|
| `execute()`를 문장마다 (트랜잭션 안) | 144 | 726 |
| `execute_pipeline()` (트랜잭션 안) | 46.7 | 446 |
| `execute()`를 문장마다 (트랜잭션 밖) | 141 | 752 |
| `execute_pipeline()` (트랜잭션 밖, 자체 `atomic()`) | 47.7 | 447 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-pipeline.json`](bench/results/2026-10-26-postgres-pipeline.json)</sub>
<!-- /bench:pg-pipeline -->

<!-- bench:pg-pipeline-dashboard -->
| | 집계 쿼리 5개 1회 시간 (ms) |
|---|---:|
| `execute()`를 문장마다 (트랜잭션 안) | 7.16 |
| `execute_pipeline()` (트랜잭션 안) | 6.62 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-pipeline.json`](bench/results/2026-10-26-postgres-pipeline.json)</sub>
<!-- /bench:pg-pipeline-dashboard -->

- 이 표는 위 환경 줄과 다른 실행(결과 파일의 `ci_run`)에서 같은 종류의 러너로 잰 값입니다.
- 파이프라인은 왕복 수를 줄이는 것이라, 이득은 문장 하나의 실행 시간에 비해 왕복이 클수록 커집니다. 이 러너의 PostgreSQL 은 같은 VM 의 서비스 컨테이너라 왕복이 짧은데도 PK 조회 1,000번에서 약 3배였습니다. 네트워크를 건너는 서버에서는 재지 않았습니다.
- 5,000번에서는 1.6배로 줄었습니다. CI 러너에서는 5,000번이 1,000번의 약 9.5배였으나 로컬(PG 컨테이너)에서는 문장 수에 비례했습니다. 러너의 특성으로 보이며 원인은 재지 않았습니다.
- 대시보드처럼 문장마다 서버 실행(20,000행 집계)이 시간 대부분이면 차이가 작습니다(약 8%). 파이프라인은 서버에서 문장을 동시에 실행하지 않습니다.
- 트랜잭션 밖에서는 `execute_pipeline()`이 자체 `atomic()`을 열어 `COMMIT` 왕복이 하나 늘지만, 표에서 차이가 보이지 않을 만큼 작습니다.

### 9. CSV 내보내기: `copy_to()` 대 행 읽기 + `csv.writer` (PostgreSQL, 컬럼 7개)

표 전체(헤더 포함)를 메모리 버퍼에 CSV 바이트로 씁니다. `copy_to()`는 서버가 쓴 바이트를 모으고, 나머지는 행을 읽어 `csv.writer`로 씁니다. 출력 텍스트는 세부가 다릅니다(서버는 `t`/`f`·`+00`, `csv.writer`는 파이썬 `str()`). tracemalloc 에는 출력 버퍼(20,000행에 약 1.3 MB)도 들어갑니다. 위 환경과 다른 실행(다른 러너 CPU)에서 쟀습니다.

<!-- bench:env-postgres-export -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · INTEL(R) XEON(R) PLATINUM 8573C (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres-export -->

<!-- bench:pg-export -->
| | 20,000행 시간 (ms) | 20,000행 tracemalloc 피크 (MiB) | 200,000행 시간 (ms) | 200,000행 tracemalloc 피크 (MiB) |
|---|---:|---:|---:|---:|
| Django `values_list()` + `csv.writer` | 70.6 | 11.0 | 755 | 110.4 |
| Django `values_list().iterator(chunk_size=2000)` + `csv.writer` | 79.3 | 4.1 | 777 | 40.0 |
| 브리지 `execute()` + `csv.writer` | 70.4 | 11.0 | 751 | 110.3 |
| 브리지 `stream(chunk_size=2000)` + `csv.writer` | 74.0 | 4.1 | 766 | 40.0 |
| 브리지 `copy_to()` (COPY TO STDOUT) | 22.1 | 1.6 | 237 | 12.3 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-export.json`](bench/results/2026-10-26-postgres-export.json)</sub>
<!-- /bench:pg-export -->

`copy_to()`는 200,000행에서 237 ms 로 `execute()`·`stream()` + `csv.writer`(751·766 ms)의 3분의 1 이하였습니다. 파이썬 행을 만들지 않아 tracemalloc 피크는 출력 버퍼 수준(12.3 MiB, `stream()` + `csv.writer`는 40.0 MiB)입니다. 행을 쥐는 `execute()`·`values_list()`는 110 MiB 입니다.

### 브리지가 도움이 안 되는 경우

- **일반 CRUD.** 한 행을 읽고 쓰는 일은 `bindparam` 문장을 재사용하면 Django ORM 보다 빠르지만 매번 문장을 만들면 조금 느립니다(시나리오 4). 속도 차이 말고는 얻는 것이 없으니 signal·`save()`·검증이 따라오는 ORM 을 쓰세요.
- **대량 INSERT 만 필요하고 생성된 id 가 필요 없을 때, django-pgbulk 를 이미 쓴다면.** PostgreSQL 에서는 COPY 가 가장 빠르고, 브리지 `bulk_insert(method="copy")`와 django-pgbulk `copy()`는 같은 부류입니다(200,000행에서 1,925 ms 대 2,297 ms, binary 2,069 ms, 시나리오 2. 입력 만들기는 빼고 잼). 브리지 `bulk_insert()`(unnest)는 그 두 배 가까이 걸리고 Django `bulk_create()`보다는 25% 안팎 빠릅니다. 바꿀 이유는 속도보다 SQLAlchemy 식이 필요한지에 있습니다.
- **django-pgbulk 를 이미 쓴다면.** PK 기준 대량 UPDATE 에서 브리지가 더 빠르긴 하지만 둘 다 Django `bulk_update`보다 한 자릿수 이상 빠른 같은 부류입니다(시나리오 1). 바꿀 이유는 속도보다 SQLAlchemy 식이 필요한지에 있습니다.
- **ORM 으로 쓸 수 있는 리포트 쿼리.** CTE·윈도 함수 쿼리는 실행 시간이 DB 에서 정해져 도구 차이가 거의 없고, 이 예처럼 Django ORM(CTE 없이 `Window`로)이나 django-cte 로 표현되면 그대로 두면 됩니다(시나리오 6). 브리지는 ORM 으로 쓰기 어려운 식(여러 CTE, `LATERAL`, 복잡한 조인)에서 의미가 있습니다.
- **SQLite 에서 Django 트랜잭션이 필요 없을 때.** SQLite 에서는 SQLAlchemy 를 단독으로(별도 엔진) 쓰는 편이 대량 INSERT·읽기에서 브리지보다 빠릅니다(시나리오 2·3). 브리지는 값을 Django 필드 변환기로 바꾸는 비용을 내는 대신 ORM 과 같은 값·같은 트랜잭션을 줍니다.
- **여러 행 업서트를 ORM `bulk_create(update_conflicts=True)`로 쓸 수 있을 때.** 입력마다 달라지는 여러 행 `VALUES` 문장은 호출마다 컴파일되어, 있는 행을 잠가 읽고 `bulk_create(update_conflicts=True)`하는 ORM 이 1.4~2.3배 빨랐습니다(위 "같은 요청, 두 구현"). 브리지는 기존 값에 더하는 갱신을 한 문장으로 해야 할 때의 선택지입니다.
- **SQLite executemany 형태의 `execute(stmt, [params…])`.** 행마다 SQLAlchemy 로 파라미터를 만들기 때문에 `bulk_update()`·`bulk_insert()`보다 느립니다. 대량 쓰기는 `bulk_*`를 쓰세요(시나리오 1·2).

## 지원 대상

SQLite, PostgreSQL(psycopg 3), MySQL 8.4 LTS(mysqlclient; Django 5.2·6.1 모두 지원하는 버전), Oracle(oracledb thin 모드) · Django 5.2 이상 · SQLAlchemy 2.0 이상 2.2 미만(Oracle 의 JSON 키·경로는 2.1 이상) · Python 3.12 이상

PostgreSQL 은 Django 기본(클라이언트측 바인딩)뿐 아니라 `OPTIONS={"server_side_binding": True, "prepare_threshold": 5}`(서버측 바인딩, prepared statement)에서도 동작합니다(CI 가 두 설정 모두 돌립니다).

Oracle 은 Django 5.2·6.1 이 19c 이상을 받지만, 검증한 버전은 CI 의 Oracle 23ai Free(`gvenzl/oracle-free:23-slim-faststart`)뿐입니다. 무료 19c 이미지가 없어 19c 에서는 돌려 보지 않았고, `SELECT` 목록의 불리언 식은 19c 에 없습니다.

MariaDB 는 컴파일과 `RETURNING` 판정(INSERT·DELETE 는 되고 UPDATE 는 안 됨)만 맞춰 두었고 CI 에서 돌리지 않습니다. 로컬에서 MariaDB 11.4 로 테스트를 한 번 돌렸을 때 JSON 키·문서 비교가 ORM 과 다르게 나오고(MariaDB 의 JSON 은 텍스트이고 ORM 은 양쪽에 `JSON_UNQUOTE`를 씌움) `LATERAL`이 없었습니다. JSON 컬럼을 비교하는 쿼리는 MariaDB 에서 ORM 결과와 대조해 쓰세요.

## 라이선스

MIT
