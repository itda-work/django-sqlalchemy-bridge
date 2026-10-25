# django-sqlalchemy-bridge

> ⚠️ **실험적 프로젝트입니다 (Experimental).** API·동작·지원 범위가 예고 없이 바뀔 수 있고, 운영 환경에서 쓰는 것을 권하지 않습니다.
>
> **상태: 설계 단계 (0.0.x).** 이름을 확보하고 설계를 공개한 버전입니다. 저장소 `main`에 첫 API(모델 → `sa.Table`, `execute`, `stream`)가 들어가고 있지만 아직 릴리스되지 않았으니 의존하지 마세요.

Django가 연 **연결과 트랜잭션 안에서** SQLAlchemy 2.x Core 표현식(대량 DML, CTE, 윈도 함수, 복잡한 조인)을 쓰게 해 주는 라이브러리입니다.

## 왜 만드나

[aldjemy](https://github.com/aldjemy/aldjemy)가 같은 문제를 먼저 풀었지만, SQLAlchemy 내부 비공개 풀 구조에 기대 Django 연결을 공유하는 탓에 SQLAlchemy 2 미만에 묶여 있습니다. Django 6.0부터는 SQLAlchemy 버전과 상관없이 모델 변환 단계에서도 깨집니다. 이 프로젝트는 다른 길을 갑니다.

## 설계 원칙

- **연결과 트랜잭션은 Django가 관리합니다.** SQLAlchemy는 SQL을 만들고 컴파일하는 데만 쓰고, 실행은 항상 Django 커서로 합니다. 그래서 `atomic()`, savepoint, `on_commit`, `execute_wrapper`, `assertNumQueries`, Django 연결 풀, PgBouncer 설정이 그대로 동작합니다.
- **SQLAlchemy 공개 API를 우선합니다.** 공개 API만으로 안 되는 부분(외부에서 받은 행으로 SQLAlchemy `Result`를 만드는 경로)은 어댑터 모듈 한 곳에 가두고, 지원하는 SQLAlchemy 버전 범위를 좁게 고정해 CI로 지킵니다.
- **테이블 구조의 기준은 Django 모델 하나입니다.** `sa.Table`은 Django 모델에서 만들고, 마이그레이션은 Django가 맡습니다.
- **값 변환은 Django 필드를 따릅니다.** 쓸 때는 `get_db_prep_value`, 읽을 때는 Django ORM이 쓰는 변환기를 거칩니다. 그래서 JSON·UUID·시간대가 있는 datetime·Decimal·Enum이 ORM과 똑같이 왕복합니다. (SQLAlchemy 타입 변환을 그대로 쓰면 SQLite에서 시간이 9시간 밀리거나 Enum이 값 대신 이름으로 저장되는 문제가 있어 피합니다.)
- **경계를 드러냅니다.** 브리지로 쓴 내용은 Django signal을 발생시키지 않습니다. API 이름과 문서에 이 점을 분명히 적습니다.

## 모델에서 테이블 만들기 (개발 중, 미릴리스)

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
- `compare_table(model, table=None, check_db=False)`는 차이를 `Difference(source, kind, column, expected, actual)` 리스트로 돌려줍니다. 모델과는 테이블명·컬럼·타입(`FieldType`이면 감싼 타입)·nullable·PK·FK 를, 실제 DB 와는 컬럼 유무와 nullable 을 비교합니다(DB 타입 비교는 예정).
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
| `JSONField` | `JSON` |
| `BinaryField` | `LargeBinary` |
| `ForeignKey`, `OneToOneField` | 대상 컬럼의 타입 + `ForeignKey` |

- `choices`가 있는 필드는 바탕 필드의 타입을 그대로 씁니다.
- `ManyToManyField`와 역관계는 컬럼이 아니므로 건너뜁니다(중간 테이블 모델은 따로 `table_for` 할 수 있습니다). 다중 테이블 상속의 자식 테이블에는 자기 컬럼과 `<부모>_ptr_id`만 들어갑니다. 복합 PK(`CompositePrimaryKey`)를 반영합니다.
- 위 표에 없는 필드가 DB 컬럼을 가지면(예: PostgreSQL `ArrayField`, 직접 만든 필드) 조용히 넘기지 않고 `UnsupportedFieldError`를 냅니다.

## 실행하기 (개발 중, 미릴리스)

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
- 컬럼으로 추적되지 않는 값(`LIKE` 패턴, 산술식, `count()` 같은 식, `sa.literal()`, `sa.text()`)은 변환 없이 그대로 오갑니다.
- 리터럴 `%`는 그대로 써도 됩니다(`LIKE 'a%'`, `text("SELECT '100%'")`). 브리지가 `%s` 자리표시로 컴파일하고 `%`를 `%%`로 바꾼 뒤, 파라미터가 없어도 빈 튜플을 넘겨 백엔드가 되돌리게 합니다.
- async 코드에서 직접 부르면 ORM처럼 `SynchronousOnlyOperation`이 납니다. `sync_to_async`로 감싸세요.

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

> ⚠️ **브리지로 쓴 내용은 모델 계층을 거치지 않습니다.** `save()`·`delete()`가 불리지 않고, `pre_save`/`post_save`/`pre_delete`/`post_delete` 등 **signal이 발생하지 않으며**, Django 필드의 `default`·`auto_now`도 채워지지 않습니다. 값은 모두 직접 넣어야 합니다.

## 비동기

Django의 async ORM(`aget()` 등)은 내부적으로 쿼리마다 `sync_to_async`로 스레드에 넘기는 구조이고, Django 문서도 "async 모드에서는 아직 트랜잭션이 동작하지 않는다"고 적고 있습니다.

- **v0.1은 `arun(fn)` 하나만 제공합니다.** 동기 함수 `fn`(여러 쿼리와 `atomic()`을 담은 작업 단위)을 `sync_to_async`로 **한 번에** 스레드에 넘겨 실행합니다. 트랜잭션은 그대로 Django가 관리하고, async 코드에서도 트랜잭션을 쓸 수 있습니다. 얻는 것은 스레드 위임 횟수가 줄어드는 것이지, 진짜 비동기 I/O는 아닙니다.
- **`async with atomic()` 형태는 넣지 않습니다.** 같은 연결을 여러 코루틴이 동시에 쓰면 다른 코루틴의 쿼리가 그 트랜잭션에 섞여, 오류 없이 함께 롤백될 수 있기 때문입니다.
- **SQLAlchemy 비동기 엔진(`AsyncEngine`)과 psycopg 3 비동기 연결 직접 사용은 채택하지 않습니다.** 둘 다 Django 밖에서 따로 연결을 열기 때문에 Django 트랜잭션·잠금·쿼리 집계(`execute_wrapper`, `assertNumQueries`) 밖에서 돕니다. "연결과 트랜잭션은 Django가 관리한다"는 원칙과 맞지 않습니다.

## 성능과 메모리 (측정 중)

이 README의 모든 수치는 저장소의 `bench/` 스크립트와 결과 파일로 다시 재현할 수 있을 때만 싣습니다. 측정할 항목은 아래와 같습니다.

- PK 기준 대량 UPDATE: Django `bulk_update`, django-pgbulk, 브리지, SQLAlchemy ORM
- 대량 INSERT: `bulk_create`, pgbulk `copy`, 브리지, SQLAlchemy `insert()`
- 대량 읽기의 시간과 메모리: Django 모델 객체, `values()`, `values_list()`, 브리지, SQLAlchemy `Row`, SQLAlchemy ORM 객체
- 쿼리 생성 오버헤드, async 경로, CTE·윈도 함수 리포트 쿼리

브리지가 도움이 **안 되는** 경우(일반 CRUD, django-pgbulk로 충분한 경우)도 함께 적습니다.

## 첫 지원 대상 (계획)

SQLite, PostgreSQL(psycopg 3) · Django 5.2 이상 · SQLAlchemy 2.0 이상 2.2 미만 · Python 3.12 이상

## 라이선스

MIT
