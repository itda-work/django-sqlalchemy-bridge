# django-sqlalchemy-bridge

> ⚠️ **실험적 프로젝트입니다 (Experimental).** API·동작·지원 범위가 예고 없이 바뀔 수 있고, 운영 환경에서 쓰는 것을 권하지 않습니다.
>
> **상태: 0.1 (알파).** 첫 API(모델 → `sa.Table`, `execute`, `stream`, `bulk_update`/`bulk_insert`, `arun`)를 담은 첫 릴리스입니다. 0.x 동안은 마이너 버전 사이에도 API가 바뀔 수 있으니 버전을 고정해 쓰세요.

Django가 연 **연결과 트랜잭션 안에서** SQLAlchemy 2.x Core 표현식(대량 DML, CTE, 윈도 함수, 복잡한 조인)을 쓰게 해 주는 라이브러리입니다.

## 왜 쓰나

**표현력 때문입니다.** Django ORM 으로 쓰기 어려운 SQL(재귀 CTE, 여러 CTE, `INSERT … ON CONFLICT … RETURNING`, `UPDATE … FROM`, `INSERT … SELECT`, PostgreSQL 의 `LATERAL`·`DISTINCT ON`·데이터를 바꾸는 CTE)을 raw SQL 문자열 대신 SQLAlchemy 식으로 씁니다. 그러면서도 연결·트랜잭션·값 변환은 Django 의 것을 그대로 씁니다. `atomic()` 안에서 ORM 쿼리와 섞여 돌고, 결과의 datetime·Decimal·JSON 은 ORM 이 읽은 값과 같습니다. raw SQL 과 달리 테이블·컬럼 이름이 모델에서 나오므로 오타가 실행 전에 드러나고, 백엔드마다 SQL 을 따로 쓰지 않아도 되는 경우가 많습니다.

성능은 목적이 아닙니다. 대량 쓰기에서는 따라옵니다. `bulk_update`는 Django `bulk_update`보다 한 자릿수 이상, `bulk_insert`는 Django `bulk_create`보다 같거나 조금 빠릅니다. 그 밖의 쿼리는 실행 시간이 DB 에서 정해져 ORM 과 거의 같습니다(아래 "성능과 메모리").

**언제 ORM 을 쓰나.** ORM 으로 자연스럽게 써지는 쿼리, 한 행씩 읽고 쓰는 CRUD, signal·`save()`·필드 `default`·검증이 따라와야 하는 쓰기는 ORM 을 쓰세요. 브리지의 쓰기는 모델 계층을 거치지 않습니다(아래 "대량 쓰기"의 경고). 자세한 경우는 "[브리지가 도움이 안 되는 경우](#브리지가-도움이-안-되는-경우)"에 있습니다.

### 예제

아래 코드는 테스트(`tests/test_examples.py`)에서 그대로 돌고, README 가 그 코드와 어긋나면 테스트가 실패합니다. 모델은 작은 상점([`tests/testapp/models.py`](tests/testapp/models.py)의 `Category`·`Product`·`Sale`·`DailyStock`·`PriceChange`)이고, 테스트 파일에는 LATERAL·DISTINCT ON·여러 CTE·윈도 함수·`INSERT … SELECT`·`ON CONFLICT DO NOTHING`·데이터를 바꾸는 CTE 예제가 더 있습니다. 모든 예제는 SQLite·PostgreSQL 에서 돌고, PostgreSQL 전용 문법만 SQLite 에서 건너뜁니다. 예제 코드의 import 는 `import datetime`, `from zoneinfo import ZoneInfo`, `import sqlalchemy as sa`, `from django.db import connection, transaction`, `from sqlalchemy.dialects import postgresql, sqlite`, `from django_sqlalchemy_bridge import execute, table_for`입니다.

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

`UPDATE … FROM`: 따로 올려 둔 가격표를 바뀐 행에만 적용하고 바뀐 행을 돌려받습니다(SQLite 3.33+, PostgreSQL).

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
- **SQLAlchemy 공개 API를 우선합니다.** 공개 API만으로 안 되는 부분(외부에서 받은 행으로 SQLAlchemy `Result`를 만드는 경로)은 어댑터 모듈 한 곳에 가두고, 지원하는 SQLAlchemy 버전 범위를 좁게 고정해 CI로 지킵니다.
- **테이블 구조의 기준은 Django 모델 하나입니다.** `sa.Table`은 Django 모델에서 만들고, 마이그레이션은 Django가 맡습니다.
- **값 변환은 Django 필드를 따릅니다.** 쓸 때는 `get_db_prep_value`, 읽을 때는 Django ORM이 쓰는 변환기를 거칩니다. 그래서 JSON·UUID·시간대가 있는 datetime·Decimal·Enum이 ORM과 똑같이 왕복합니다. (SQLAlchemy 타입 변환을 그대로 쓰면 SQLite에서 시간이 9시간 밀리거나 Enum이 값 대신 이름으로 저장되는 문제가 있어 피합니다.)
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
  - 그 밖의 백엔드에서는 타입을 비교하지 않습니다.
- 다중 DB 는 아직 `default` 만 받습니다(`using` 인자 자리만 있음).
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
| `JSONField` | `postgresql.JSONB` (SQLite 에서는 `JSON` 으로 컴파일) |
| `BinaryField` | `LargeBinary` |
| `ForeignKey`, `OneToOneField` | 대상 컬럼의 타입 + `ForeignKey` |

- `choices`가 있는 필드는 바탕 필드의 타입을 그대로 씁니다.
- `JSONField`는 `postgresql.JSONB().with_variant(sa.JSON(), "sqlite")`입니다. 연산자(`contains()`·`has_key()` 등)는 dialect 를 고르기 전, 식을 만들 때의 타입에서 나오므로 기본 타입이 `JSONB`여야 PostgreSQL 연산자가 열립니다(`sa.JSON` 기본에 PostgreSQL 변형만 붙이면 `contains()`가 조용히 `LIKE`가 됩니다). 그래서 `compare_table`은 직접 만든 테이블의 `sa.JSON()` 컬럼을 타입 차이로 보고합니다.
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

- `execute(stmt, params=None, *, using="default")`는 `connections[using]`의 vendor에 맞는 SQLAlchemy dialect(SQLite, PostgreSQL+psycopg)로 컴파일하고 **Django 커서로** 실행합니다. 그래서 `atomic()`·savepoint·`on_commit`·`execute_wrapper`·`CaptureQueriesContext`·`assertNumQueries`가 ORM 쿼리와 똑같이 적용됩니다.
- **반환 형태**: 행을 돌려주는 문장(`SELECT`, `RETURNING`)은 튜플 리스트, 그 밖의 문장은 커서의 rowcount(정수)입니다. `params`에 매핑을 넘기면 `bindparam()` 값(그리고 `insert(table)`처럼 값이 없는 INSERT의 컬럼)을 채우고, 매핑의 시퀀스를 넘기면 `cursor.executemany()`로 실행합니다(이때 `RETURNING`은 받지 않습니다).
- `execute_result(stmt, params=None, *, using="default")`는 같은 행을 SQLAlchemy `Result`로 줍니다(`row.name`, `row._mapping`, `.mappings()`, `.scalars()`). 행을 모두 가져와 변환한 뒤의 `Result`입니다. 외부 행으로 `Result`를 만드는 공개 API가 없어 이 부분만 `_sa_result.py` 한 곳에서 비공개 API를 씁니다.
- **값 변환은 Django 필드 기준**입니다. 컬럼과 비교(`==`, `!=`, `<`, `IN`, `BETWEEN` 등)하거나 INSERT/UPDATE로 넣는 값은 `field.get_db_prep_value()`를, 컬럼을 고른 결과(라벨·`max()`처럼 컬럼 타입을 그대로 잇는 식 포함)는 ORM과 같은 변환기(`connection.ops.get_db_converters()` + 필드 변환기)를 거칩니다. JSON·UUID·시간대 있는 datetime(SQLite에서도 UTC로 저장, 9시간 밀리지 않음)·Decimal·선택지(이름이 아니라 값)가 ORM과 똑같이 왕복합니다. `None`은 ORM `save()`처럼 SQL `NULL`입니다.
- 컬럼으로 추적되지 않는 값(`LIKE` 패턴, 산술식, `count()` 같은 식, `sa.literal()`, `sa.text()`)은 변환 없이 그대로 오갑니다. `INSERT … SELECT`의 SELECT 목록에 넣는 값도 여기에 듭니다. 브리지는 그 값이 들어갈 대상 컬럼을 알지 못하므로(SQLAlchemy 가 `from_select()`의 컬럼 이름을 공개 API 로 내주지 않음) 값마다 대상 컬럼 타입을 주세요: `sa.literal(value, daily_stock.c.counted_at.type)`, `sa.bindparam("at", type_=daily_stock.c.counted_at.type)`. 타입이 없으면 UUID(SQLite)·JSON 컬럼에 넣는 dict(PostgreSQL)는 드라이버에서 에러가 나고, 시간대 있는 datetime 은 SQLite 에서 에러 없이 UTC 가 아닌 오프셋 문자열로 저장돼 ORM 필터가 그 행을 찾지 못합니다. 예외는 JSON 키·경로(`meta["k"]`, `meta[0]`, `meta[("a", "b")]`)로, 값이 아니라 문법이라 dialect 표기(SQLite `'$."k"'`, PostgreSQL `'{a, b}'`)로 바꿔 넘깁니다.
- **JSON 컬럼.** `meta["k"] == 1`(ORM `meta__k=1`), `meta[("a", "b")] == 1`, `meta["k"].as_string()`은 두 백엔드에서 동작합니다. PostgreSQL에서는 jsonb 연산자도 쓸 수 있고 결과가 ORM 조회와 같습니다(테스트로 확인).

  | 브리지 (PostgreSQL) | ORM |
  |---|---|
  | `meta.contains({"k": 1})` (`@>`) | `meta__contains={"k": 1}` |
  | `meta.contained_by({...})` (`<@`) | `meta__contained_by={...}` |
  | `meta.has_key("k")` / `has_all([...])` / `has_any([...])` | `meta__has_key` / `meta__has_keys` / `meta__has_any_keys` |
  | `meta["k"].astext == "1"` (`->>`) | `annotate(v=KT("meta__k")).filter(v="1")` |

  `contains`·`contained_by`의 우변은 컬럼 값과 같게 `JSONField.get_db_prep_value()`로 jsonb 바인딩합니다. SQLite에는 이 연산자들이 없습니다. `contains`·`has_key`는 실행 시 `OperationalError`, `astext`(`->>`)는 SQLite 3.38+의 `->>`로 실행되지만 텍스트가 아니라 SQL 값을 돌려줘 `{"k": 1}`이 `'1'`과 같지 않습니다. SQLite에서 `meta["k"] == [1, 2]`처럼 구조 값과 비교하면 JSON 텍스트끼리 비교라 공백이 다르면 맞지 않습니다(ORM 은 `JSON()`으로 정규화). 스칼라 비교는 ORM 과 같습니다.
- **컴파일 캐시: `bindparam()`으로 만든 문장을 재사용하면 컴파일을 건너뜁니다.** 브리지는 넘겨받은 문장 객체마다 컴파일 결과를 기억해, 같은 객체를 같은 DB 종류(vendor)에서 같은 이름의 `params`로 다시 실행하면 컴파일 없이 값 변환과 실행만 합니다(`execute`·`execute_result`·`stream` 공통). 그러니 값은 `sa.bindparam("pk")`로 비워 둔 문장을 한 번 만들어 두고(모듈 수준 등) 돌려 쓰세요. `where(orders.c.id == pk)`처럼 값을 넣어 매번 새로 만든 문장은 매번 컴파일합니다(아래 "성능과 메모리" 4번). `params` 이름이 달라지면(예: UPDATE 의 `SET` 컬럼) SQL 이 달라지므로 따로 컴파일합니다. 결과 변환기와 `RETURNING` 지원 검사는 연결(alias)마다 다를 수 있어 매 호출 그 연결로 다시 정합니다. 기억한 결과는 문장 객체가 사라지면 함께 사라지고, 여러 스레드가 같은 문장을 써도 됩니다. 끄는 스위치는 없습니다(결과가 캐시 여부와 무관하므로).
- 리터럴 `%`는 그대로 써도 됩니다(`LIKE 'a%'`, `text("SELECT '100%'")`). 브리지가 `%s` 자리표시로 컴파일하고 `%`를 `%%`로 바꾼 뒤, 파라미터가 없어도 빈 튜플을 넘겨 백엔드가 되돌리게 합니다.
- async 코드에서 직접 부르면 ORM처럼 `SynchronousOnlyOperation`이 납니다. 작업 단위를 동기 함수로 묶어 `arun`으로 부르세요(아래 "비동기").
- DRF·클래스 기반 뷰(`ViewSet`·`APIView`·페이지네이션·`TemplateView`)에서 쓰는 레시피와 되지 않는 것(`ModelViewSet`·`ModelSerializer`·필터 백엔드·`CursorPagination` 등)은 [docs/drf.md](docs/drf.md)에 있습니다.

### 쪼개 읽기: `stream`

```python
from django_sqlalchemy_bridge import stream

for order_id, paid_at in stream(sa.select(orders.c.id, orders.c.paid_at), chunk_size=2000):
    ...
```

- `stream(stmt, params=None, *, chunk_size=2000, using="default")`는 행을 `chunk_size`개씩 가져와 튜플로 하나씩 내주는 제너레이터입니다. 값 변환은 `execute`와 같습니다. 기본 `chunk_size`는 Django `QuerySet.iterator()`의 기본값(2000)과 같습니다.
- 커서는 `QuerySet.iterator()`와 같은 경로로 엽니다. `connection.chunked_cursor()`(PostgreSQL에서는 서버 측 named cursor, SQLite에서는 일반 커서)에 `fetchmany(chunk_size)`를 반복합니다. 데이터베이스 설정에 `DISABLE_SERVER_SIDE_CURSORS: True`가 있으면 일반 커서를 씁니다. `CaptureQueriesContext`·`assertNumQueries`에는 `iterator()`처럼 1건으로 잡힙니다(`FETCH`는 잡히지 않습니다).
- 첫 행을 요청할 때 실행됩니다. 행이 끝나거나, 제너레이터를 `close()`하거나(`for` 문을 `break`로 빠져나가 제너레이터가 버려질 때 포함), 예외로 끝나면 커서를 닫습니다. 제너레이터를 변수에 쥔 채 소비를 멈추면 닫히지 않으니 `close()`를 부르거나 `contextlib.closing()`으로 감싸세요.
- 행을 돌려주지 않는 문장(`RETURNING` 없는 DML)은 실행 전에 `ValueError`, 매핑의 시퀀스(executemany 형태)는 `TypeError`입니다. 행이 없는 `text()` 문장은 첫 `next()`에서 `ValueError`입니다.
- PostgreSQL의 서버 측 커서(`DECLARE`)는 `SELECT`·`VALUES`만 받습니다. 그래서 `RETURNING`이 있는 DML은 일반 커서로 실행하고(행은 드라이버가 한꺼번에 받으므로 메모리 이득은 없습니다), `text()` 문장도 `SELECT`·`VALUES`여야 합니다. 데이터를 바꾸는 CTE(`WITH u AS (UPDATE ... RETURNING ...) SELECT ...`)는 PostgreSQL이 `NotSupportedError`로 거부하니 `execute`를 쓰세요.

**PostgreSQL에서 트랜잭션 밖(autocommit)일 때.** Django는 autocommit 상태에서 named cursor를 `WITH HOLD`로 엽니다(`atomic()` 안에서는 일반 커서). 저장소에서 확인한 동작은 이렇습니다(PostgreSQL 18, psycopg 3).

- `WITH HOLD` 커서는 `DECLARE`가 커밋되는 시점에 **결과 전체를 서버에서 계산해 서버 쪽에 저장합니다.** 그래서 첫 실행이 쿼리 전체만큼 걸리고(이후 `fetchmany`는 저장된 결과를 읽습니다), 결과가 크면 서버의 임시 저장 공간을 그만큼 씁니다. 클라이언트 메모리는 여전히 청크 단위입니다. `atomic()` 안에서는 결과를 미리 만들지 않고 `fetchmany`마다 계산합니다.
- 커서는 그 뒤 다른 트랜잭션이 커밋돼도 살아 있고, 만들 때의 결과를 그대로 줍니다. 닫지 않으면 연결이 닫힐 때까지 서버에 남습니다.
- 반대로 `atomic()` 안에서 연 스트림은 그 트랜잭션이 끝나면 서버 커서가 사라집니다. 블록을 나온 뒤 계속 소비하면 `ProgrammingError: cursor ... does not exist`가 납니다. 블록 안에서 다 읽으세요.
- PgBouncer를 transaction pooling 모드로 쓰면 트랜잭션이 끝난 뒤의 `FETCH`가 커서를 연 것과 다른 서버 연결로 갈 수 있으니, Django 문서대로 `DISABLE_SERVER_SIDE_CURSORS: True`를 설정해야 합니다. 그러면 `stream`도 일반 커서를 씁니다(드라이버가 결과를 한꺼번에 받습니다).

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
```

- `bulk_update(table, rows, *, key=None, columns=None, batch_size=None, using="default")`, `bulk_insert(table, rows, *, returning=None, batch_size=None, using="default")`. `table`은 `table_for()` 테이블이나 모델입니다. `rows`는 컬럼 이름(`db_column`, FK 는 `<이름>_id`)을 키로 한 매핑의 시퀀스입니다.
- **PostgreSQL에서는 문장 하나입니다.** 컬럼마다 배열 파라미터 하나를 Django 컬럼 타입으로 캐스트하고(`(%s)::jsonb[]`, `(%s)::numeric[]` …) `unnest()`로 행을 만듭니다. UPDATE 는 `UPDATE t SET c = v.c FROM unnest(...) AS v(...) WHERE t.k = v.k`, INSERT 는 `INSERT INTO t (...) SELECT * FROM unnest(...)` 꼴로, Django `bulk_create`가 PostgreSQL에서 쓰는 방식과 같습니다. 캐스트에서는 길이·자릿수를 떼므로(`varchar(50)[]`가 아니라 `varchar[]`) 값이 잘리지 않고, 너무 긴 값은 ORM 쓰기처럼 컬럼에 넣을 때 오류가 납니다. `CaptureQueriesContext`에는 1건으로 잡힙니다.
- **그 밖의 백엔드(SQLite)에서는 같은 호출이 `cursor.executemany()`입니다(`atomic()` 안에서, 아래 "원자성").** `returning`이 있는 INSERT 는 executemany 로 행을 받을 수 없어서 다중 `VALUES` 문장을 `connection.ops.bulk_batch_size()`(SQLite 변수 한도) 단위로 나눠 실행합니다. 결과는 PostgreSQL과 같습니다.
- 값 변환은 `execute`와 같습니다. 행 값마다 `field.get_db_prep_value()`(`None`은 SQL `NULL`), `returning` 컬럼은 ORM 변환기를 거칩니다. 파라미터는 행마다 SQLAlchemy로 만들지 않고 컬럼마다 리스트 하나로 바로 만듭니다.
- **빠진 값은 오류입니다.** 실행 전에 `ValueError`를 냅니다.
  - `bulk_update`: 키나 설정할 컬럼이 없는 행, 키가 같은 두 행(PostgreSQL은 그중 아무 행 하나로, executemany 는 둘 다 차례로 갱신해 결과가 달라지므로 거부). `columns`를 생략하면 첫 행의 키 외 컬럼을 설정하고 모든 행이 같은 키를 가져야 합니다. `columns`를 주면 행의 다른 키는 무시합니다. 없는 키 값은 아무 행도 바꾸지 않고, rowcount 는 맞은 행만 셉니다.
  - `bulk_insert`: 행끼리 키가 다르거나, 테이블 컬럼을 빠뜨린 경우. 자동 증가 PK 와 `db_default`가 있는 컬럼만 빠뜨릴 수 있습니다. Django 필드 `default`·`auto_now`를 채우지 않으므로 nullable 컬럼도 `None`을 직접 넣어야 합니다(잊은 컬럼이 조용히 `NULL`이 되지 않게).
- **`batch_size`**: 양의 정수면 그 행 수마다 문장 하나(SQLite 는 `executemany()` 호출 하나)로 나눕니다. `None`(기본값)이면 위와 같습니다. 0 이하나 정수가 아니면 실행 전에 `ValueError`입니다. SQLite 의 `returning` 배치는 `bulk_batch_size()`를 넘지 않습니다. 키 중복·누락 검사는 나누기 전에 전체 행으로 하고, rowcount 는 배치의 합, `returning` 결과는 배치와 상관없이 `rows` 순서입니다.
- **원자성**: 문장을 둘 이상 실행하는 호출(배치가 여럿이거나 SQLite `executemany()`)은 Django `bulk_create`·`bulk_update`처럼 `transaction.atomic(using=..., savepoint=False)` 안에서 돕니다. 한 배치가 실패하면 그 호출이 쓴 행은 모두 롤백됩니다. 바깥 `atomic()` 안에서는 savepoint 를 만들지 않으므로, 실패를 잡아 트랜잭션을 이어가려면 호출을 `atomic()`으로 한 번 더 감싸세요(ORM 쓰기와 같습니다).
- **언제 `batch_size`를 주나.** PostgreSQL 의 한 문장은 행 수에 비례해 클라이언트(파라미터 배열)와 서버 백엔드(배열과 `unnest` 결과) 메모리를 씁니다. 1,000,000행(컬럼 6개)에서 `bulk_insert`는 클라이언트 tracemalloc 피크 849 MiB, 서버 백엔드 233 MiB 였고, `batch_size=10_000`이면 226 MiB·6 MiB 로 줄면서 시간은 4% 빨랐습니다. `bulk_update`는 나누면 느려집니다(1,000,000행에서 `10_000`은 22% 느림, `200_000`은 3% 느림). 한 문장은 대상 테이블과 해시 조인 한 번으로, 작은 배치는 행마다 PK 인덱스 조회로 갱신하기 때문입니다(로컬 PostgreSQL 18 의 `EXPLAIN ANALYZE`로 확인). 그래서 기본값은 `None`이고, 수십만 행 이상에서 메모리가 문제일 때 `bulk_insert`는 `10_000`~`50_000`, `bulk_update`는 `200_000` 안팎을 권합니다. SQLite 는 차이가 작습니다(아래 "성능과 메모리" 7).
- 빈 `rows`는 쿼리를 실행하지 않고 `0`(또는 `returning`이면 `[]`)을 돌려줍니다.
- **`RETURNING` 순서**: 결과는 `rows` 순서입니다(테스트로 고정). 다만 PostgreSQL·SQLite 모두 `RETURNING` 순서를 문서로 보장하지는 않으며, Django `bulk_create`가 PK 를 객체에 채울 때도 같은 동작에 기댑니다. 아래 "제약"을 보세요.
- signal·`save()`를 거치지 않는 것은 다른 브리지 쓰기와 같습니다(아래).

> ⚠️ **브리지로 쓴 내용은 모델 계층을 거치지 않습니다.** `save()`·`delete()`가 불리지 않고, `pre_save`/`post_save`/`pre_delete`/`post_delete` 등 **signal이 발생하지 않으며**, Django 필드의 `default`·`auto_now`도 채워지지 않습니다. 값은 모두 직접 넣어야 합니다.

## 제약

브리지가 실행 전에 막는 것과, 막지 않거나 막을 수 없는 것들입니다.

- **RETURNING 은 그 DB 가 지원할 때만.** dialect 는 DB 에 연결하지 않고 컴파일하므로 어떤 SQLite 에도 `RETURNING`을 씁니다(SQLite 3.35 부터 지원, Django 5.2 는 3.31 부터 받음). 그래서 `RETURNING`이 있는 SQLAlchemy DML(`execute`·`execute_result`·`stream`, `bulk_insert(returning=...)`)은 실행 연결의 Django feature `can_return_columns_from_insert`가 거짓이면 **실행 전에** `django.db.NotSupportedError`를 냅니다. `sa.text("... RETURNING ...")`는 안을 볼 수 없어 검사하지 않고 DB 가 판단합니다.
- **SQLAlchemy Python 측 컬럼 `default`/`onupdate`는 적용할 수 없어 실행 전에 막습니다.** 직접 만든 `sa.Table`의 `Column(default=..., onupdate=...)`에 준 Python 값·callable 은 SQLAlchemy 실행 컨텍스트(`Engine`·`Connection`)가 채우는데 브리지는 Django 커서로 실행합니다. 컴파일러는 그 컬럼을 SQL 에 넣어 두므로(`INSERT ... (name, ...) VALUES (%s, ...)`, `UPDATE ... SET name=%s, ...`) 그대로 두면 `NULL`이 바인딩되어, nullable 컬럼에는 조용히 `NULL`이 저장되고 `onupdate` 컬럼은 UPDATE 마다 기존 값이 `NULL`로 덮입니다(SQLite·PostgreSQL 에서 확인). 그래서 그런 컬럼을 비워 둔 INSERT·UPDATE 는 `execute`·`execute_result`·`stream`·executemany 모두 **실행 전에** `ValueError`(`... Python-side default or onupdate of 테이블.컬럼 cannot be applied ...`)를 냅니다. 그 컬럼 값을 `.values()`나 params 로 직접 주면 그대로 실행합니다. `default=sa.func.now()`처럼 SQL 식인 default/onupdate 는 SQL 에 들어가므로 막지 않고 DB 가 계산합니다. DB 쪽 `server_default`·Django `db_default`도 DB 가 채우므로 영향이 없습니다. `table_for()` 테이블에는 이런 default 가 없고(Django 필드 `default`·`auto_now`도 채우지 않습니다), `bulk_*`는 `table_for()` 테이블만 받습니다.
- **PostgreSQL `standard_conforming_strings=off` 서버는 지원하지 않습니다.** 브리지는 연결하지 않고 컴파일하므로 리터럴 백슬래시를 "on"(PostgreSQL 기본값, Django 가 지원하는 모든 버전의 기본)으로 렌더합니다. "off" 서버에서는 컴파일러가 직접 쓴 리터럴(`sa.literal(..., literal_execute=True)` 등)의 백슬래시가 이스케이프로 읽혀 `'a\tb'`가 탭 문자가 됩니다(세션에서 "off"로 바꿔 PostgreSQL 18 에서 확인). 바인딩 값에는 영향이 없습니다.
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
- `fn`은 위치 전용 인자라 `fn`이나 `using`이라는 키워드 인자도 그대로 `fn`에 넘어갑니다. `arun`에는 `using`이 없습니다. 어느 DB를 쓸지는 `fn` 안의 `execute(..., using=...)`·`atomic(using=...)`이 정합니다.
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

<!-- bench:pg-insert -->
| | 20,000행 시간 (ms) | 200,000행 시간 (ms) |
|---|---:|---:|
| Django `bulk_create()` (batch_size 기본값) | 237 | 2,798 |
| Django `bulk_create(batch_size=1000)` | 225 | 2,269 |
| django-pgbulk `copy()` | 113 | 1,138 |
| django-pgbulk `copy(binary=True)` | 103 | 1,038 |
| SQLAlchemy Core `insert()` (별도 엔진) | 229 | 2,497 |
| SQLAlchemy Core `insert().returning(id)` (별도 엔진) | 440 | 5,021 |
| 브리지 `execute(insert(t), [rows...])` (executemany) | 453 | 5,110 |
| 브리지 `bulk_insert()` | 166 | 2,215 |
| 브리지 `bulk_insert(returning=['id'])` | 176 | 2,237 |

<sub>결과 파일: [`bench/results/2026-10-25-postgres-insert.json`](bench/results/2026-10-25-postgres-insert.json)</sub>
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
| Django 커서에 SQL 직접 | 488 | 97.5 |
| Django `objects.get(pk=...)` | 1,316 | 263 |
| Django `values_list(...).get(pk=...)` | 1,415 | 283 |
| SQLAlchemy Core, 문장 1회 생성 + `bindparam` (별도 엔진) | 550 | 110 |
| SQLAlchemy Core, 매번 문장 생성 (별도 엔진) | 833 | 167 |
| 브리지 `execute()`, 문장 1회 생성 + `bindparam` | 749 | 150 |
| 브리지 `execute()`, 매번 문장 생성 | 1,573 | 315 |
| (참고) 브리지 내부 `prepare()`만 (캐시 적중 + 파라미터 변환), DB 없음 | 50.7 | 10.1 |
| (참고) 내부 `prepare()` 1회 후 커서 하나로 실행·변환만 | 471 | 94.3 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-lookup.json`](bench/results/2026-10-26-postgres-lookup.json)</sub>
<!-- /bench:pg-lookup -->

<!-- bench:sqlite-lookup -->
| | 시간 (ms) | 호출당 (µs) |
|---|---:|---:|
| Django 커서에 SQL 직접 | 28.0 | 5.60 |
| Django `objects.get(pk=...)` | 501 | 100 |
| Django `values_list(...).get(pk=...)` | 620 | 124 |
| SQLAlchemy Core, 문장 1회 생성 + `bindparam` (별도 엔진) | 78.9 | 15.8 |
| SQLAlchemy Core, 매번 문장 생성 (별도 엔진) | 249 | 49.9 |
| 브리지 `execute()`, 문장 1회 생성 + `bindparam` | 118 | 23.6 |
| 브리지 `execute()`, 매번 문장 생성 | 666 | 133 |
| (참고) 브리지 내부 `prepare()`만 (캐시 적중 + 파라미터 변환), DB 없음 | 62.5 | 12.5 |
| (참고) 내부 `prepare()` 1회 후 커서 하나로 실행·변환만 | 45.6 | 9.12 |

<sub>결과 파일: [`bench/results/2026-10-26-sqlite-lookup.json`](bench/results/2026-10-26-sqlite-lookup.json)</sub>
<!-- /bench:sqlite-lookup -->

- 이 표는 위 환경 줄과 다른 실행(결과 파일의 `ci_run`)에서 같은 종류의 러너로 다시 잰 값입니다(#28 컴파일 캐시 반영).
- 브리지는 **같은 문장 객체를 다시 실행하면 컴파일을 건너뜁니다**(위 "실행하기"의 컴파일 캐시). `bindparam`으로 한 번 만든 문장은 첫 호출에만 컴파일하고, 매번 새로 만든 문장은 매번 컴파일합니다. 두 행의 차이(PostgreSQL 약 165µs, SQLite 약 110µs)가 호출당 컴파일 비용입니다. 문장을 한 번 만들어 `bindparam`으로 재사용하세요.
- "(참고) `prepare()`만" 행은 캐시 적중 뒤 남는 브리지 쪽 일(파라미터 변환, 결과 변환기 찾기)이고 호출당 약 10~13µs 입니다. "(참고) `prepare()` 1회" 행은 그것마저 한 번만 하고 커서 하나로 실행·변환만 한 값으로, 공개 API 가 아닙니다. 브리지 `execute()`는 호출마다 Django 커서를 새로 엽니다(Django `objects.get()`도 마찬가지). PostgreSQL 에서 `execute()`가 두 참고 행의 합보다 느린 몫은 그 때문으로 보이지만 따로 재지는 않았습니다.
- PostgreSQL 에서 `bindparam` 재사용 브리지(호출당 150µs)는 Django `objects.get()`(263µs)보다 빠르고, SQLAlchemy 단독 엔진(110µs)과의 차이는 40µs 입니다. SQLite 에서는 23.6µs 로 SQLAlchemy 단독(15.8µs)과 같은 자릿수이고 Django ORM 조회(100µs 이상)보다 4배 남짓 빠릅니다.

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

`batch_size`에 따른 시간, 클라이언트 tracemalloc 피크, (PostgreSQL) 서버 백엔드 메모리입니다. PostgreSQL 에서 `batch_size=None`은 한 문장입니다. 서버 백엔드 메모리는 따로 한 번 더, 새 연결에서 그 연결의 PostgreSQL 백엔드 프로세스 `/proc/<pid>/status`를 2 ms 마다 읽어 호출 중 `RssAnon`(공유 버퍼를 뺀 개인 메모리) 최댓값에서 호출 전 값을 뺀 것입니다. 200,000행 결과와 `VmHWM`은 결과 파일에 있습니다. 이 절의 표는 위 표들과 다른 실행(다른 러너 CPU)에서 쟀습니다.

<!-- bench:env-postgres-batch -->
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) · Python 3.12.3 · Django 6.1.2 · SQLAlchemy 2.1.6 · psycopg 3.3.6 · django-pgbulk 3.3.0 · django-cte 4.0.0 · AMD EPYC 7763 64-Core Processor (4코어, 메모리 16 GiB) · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 반복 5회(+예열 1회) 중 최솟값
<!-- /bench:env-postgres-batch -->

<!-- bench:pg-batch-insert -->
| | 시간 (ms) | tracemalloc 피크 (MiB) | 서버 백엔드 메모리 증가 (MiB) |
|---|---:|---:|---:|
| `bulk_insert()` (batch_size=None) | 17,395 | 848.9 | 233.0 |
| `bulk_insert(batch_size=10_000)` | 16,642 | 226.0 | 6.1 |
| `bulk_insert(batch_size=50_000)` | 16,644 | 388.9 | 26.9 |
| `bulk_insert(batch_size=200_000)` | 16,998 | 643.5 | 68.2 |
| `bulk_insert(returning=['id'])` (batch_size=None) | 18,035 | 848.9 | 235.5 |
| `bulk_insert(returning=['id'], batch_size=50_000)` | 17,053 | 259.4 | 28.9 |

<sub>결과 파일: [`bench/results/2026-10-26-postgres-batch.json`](bench/results/2026-10-26-postgres-batch.json)</sub>
<!-- /bench:pg-batch-insert -->

<!-- bench:pg-batch-update -->
| | 시간 (ms) | tracemalloc 피크 (MiB) | 서버 백엔드 메모리 증가 (MiB) |
|---|---:|---:|---:|
| `bulk_update()` (batch_size=None) | 15,843 | 609.6 | 195.4 |
| `bulk_update(batch_size=10_000)` | 19,342 | 163.1 | 12.8 |
| `bulk_update(batch_size=50_000)` | 20,003 | 299.0 | 26.5 |
| `bulk_update(batch_size=200_000)` | 16,257 | 435.9 | 55.6 |

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

### 브리지가 도움이 안 되는 경우

- **일반 CRUD.** 한 행을 읽고 쓰는 일은 `bindparam` 문장을 재사용하면 Django ORM 보다 빠르지만 매번 문장을 만들면 조금 느립니다(시나리오 4). 속도 차이 말고는 얻는 것이 없으니 signal·`save()`·검증이 따라오는 ORM 을 쓰세요.
- **대량 INSERT 만 필요하고 생성된 id 가 필요 없을 때.** PostgreSQL 에서는 django-pgbulk `copy()`(COPY)가 브리지 `bulk_insert()`보다 빠르고 메모리도 거의 쓰지 않습니다(시나리오 2). 브리지 `bulk_insert()`는 Django `bulk_create()`보다 20,000행에서 25~30% 빠르지만 200,000행에서는 `batch_size=1000`과 거의 같습니다. 브리지는 `RETURNING`이나 SQLAlchemy 식이 필요할 때의 선택지입니다.
- **django-pgbulk 를 이미 쓴다면.** PK 기준 대량 UPDATE 에서 브리지가 더 빠르긴 하지만 둘 다 Django `bulk_update`보다 한 자릿수 이상 빠른 같은 부류입니다(시나리오 1). 바꿀 이유는 속도보다 SQLAlchemy 식이 필요한지에 있습니다.
- **ORM 으로 쓸 수 있는 리포트 쿼리.** CTE·윈도 함수 쿼리는 실행 시간이 DB 에서 정해져 도구 차이가 거의 없고, 이 예처럼 Django ORM(CTE 없이 `Window`로)이나 django-cte 로 표현되면 그대로 두면 됩니다(시나리오 6). 브리지는 ORM 으로 쓰기 어려운 식(여러 CTE, `LATERAL`, 복잡한 조인)에서 의미가 있습니다.
- **SQLite 에서 Django 트랜잭션이 필요 없을 때.** SQLite 에서는 SQLAlchemy 를 단독으로(별도 엔진) 쓰는 편이 대량 INSERT·읽기에서 브리지보다 빠릅니다(시나리오 2·3). 브리지는 값을 Django 필드 변환기로 바꾸는 비용을 내는 대신 ORM 과 같은 값·같은 트랜잭션을 줍니다.
- **SQLite executemany 형태의 `execute(stmt, [params…])`.** 행마다 SQLAlchemy 로 파라미터를 만들기 때문에 `bulk_update()`·`bulk_insert()`보다 느립니다. 대량 쓰기는 `bulk_*`를 쓰세요(시나리오 1·2).

## 지원 대상

SQLite, PostgreSQL(psycopg 3) · Django 5.2 이상 · SQLAlchemy 2.0 이상 2.2 미만 · Python 3.12 이상

## 라이선스

MIT
