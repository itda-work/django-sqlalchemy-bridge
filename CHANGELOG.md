# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Oracle 지원(oracledb thin, 검증 버전 Oracle 23ai Free): `execute`·`execute_result`·`stream`·`bulk_*`·`arun`·`table_for`·`compare_table` 이 Oracle 에서 돈다. dialect 는 식별자를 Django `quote_name()` 처럼 대문자·30자(해시)로 쓴다(SQLAlchemy 기본 렌더는 `db_column="MixedCase"`·30자 넘는 컬럼·예약어 `uid` 에서 `ORA-00904`). `quoted_name(..., quote=True)` 도 대문자로 쓴다(Django 가 대소문자 구분 이름을 만들지 않으므로). `execute_result` 의 키는 Oracle 이 대문자로 돌려주는 이름을 문장의 컬럼·라벨 이름으로 되돌리고, `text()` 컬럼은 SQLAlchemy Oracle dialect 처럼 전부 대문자면 소문자로 둔다(다른 DB 의 키는 그대로). `RETURNING` 은 Oracle 에서 모두 실행 전 `NotSupportedError`(`RETURNING … INTO` 출력 바인드를 Django 커서로 받을 수 없어, 거부하지 않으면 에러 없이 rowcount 가 왔다). JSON 의 `==`·`!=` 는 `JSON_EQUAL()` 로 컴파일한다(`NCLOB` 은 `=` 로 비교할 수 없음). 문서 전체 비교는 ORM 과 같고, 키 비교는 JSON 값으로 해 `1` 과 `"1"` 을 가른다(Oracle 의 ORM 은 같게 봄). `as_string()` 등은 키가 없는 행에서 에러 대신 `NULL`. SQLAlchemy 2.0 에서는 Oracle 의 JSON 키·경로가 실행 전 `UnsupportedCompilationError`. 정밀도 없는 숫자(`sum()` 등)는 ORM 처럼 필드 타입으로 맞춘다(`DecimalField` 합이 `int` 로 오던 것). `compare_table(check_db=True)` 는 Oracle 컬럼 이름과 Django 의 빈 문자열 nullable 규칙을 따르고 타입은 비교하지 않는다. 빈 문자열 = `NULL`, `TextField`(`NCLOB`) `==` 의 `ORA-22848`, 다중 행 `VALUES` 의 `CompileError`, LOB 객체는 README "제약"에 적고 테스트로 고정했다. 테스트는 `DSB_DB=oracle`·`DSB_ORACLE_*`·`REQUIRE_ORACLE=1` 로 Oracle 에서 돌고, CI 에 Oracle 23ai Free 서비스와 `pytest (Oracle)` 단계를 넣었다. `oracledb` 는 `dev` 그룹 (#38).
- MySQL 8.4 지원(mysqlclient): `execute`·`execute_result`·`stream`·`bulk_*`·`arun`·`table_for`·`compare_table` 이 MySQL 에서 돈다. dialect 는 MySQL·MariaDB 와 연결의 `sql_mode`(`NO_BACKSLASH_ESCAPES`)마다 따로 두어 리터럴 백슬래시를 서버가 읽는 대로 쓴다. `RETURNING` 은 문장 종류별로 판정한다: MySQL 은 모두, MariaDB 는 `UPDATE … RETURNING` 만 실행 전 `NotSupportedError`(`bulk_insert(returning=...)` 포함). MySQL 의 `JSONField` 는 `JSON` 으로 컴파일하고 묶는 값을 ORM 처럼 `JSON_EXTRACT(%s, '$')` 로 감싸 JSON 문서로 비교한다(감싸지 않으면 `meta["k"] == 1` 이 `{"k": "1"}` 에 맞는다). `bulk_*` 는 executemany(UPDATE 는 행마다 문장 하나). `stream` 은 Django 처럼 일반 커서(드라이버가 결과 전체를 받음). `compare_table(check_db=True)` 는 `information_schema.columns.COLUMN_TYPE` 으로 MySQL 컬럼 타입도 비교한다(동의어·`AUTO_INCREMENT`·정수 표시 폭을 맞춰, testapp·bench 모델에서 차이 0개). MariaDB 는 컴파일과 `RETURNING` 판정만 맞추고 CI 에 넣지 않았다(타입 비교도 하지 않음). 테스트는 `DSB_DB=mysql`·`DSB_MYSQL_*`·`REQUIRE_MYSQL=1` 로 MySQL 에서 돌고, CI 에 MySQL 8.4 서비스와 `pytest (MySQL)` 단계를 넣었다. 드라이버는 `mysql` 의존성 그룹(`uv sync --group mysql`). 예제 테스트에 MySQL 업서트(`ON DUPLICATE KEY UPDATE`)·다중 테이블 UPDATE 를 더하고 README 에 업서트 예제를 실었다 (#37).

- `execute`·`execute_result`·`stream` 컴파일 캐시: 같은 문장 객체를 같은 DB vendor 에서 같은 이름의 params 로 다시 실행하면 컴파일하지 않는다(키는 문장 객체·vendor·params 이름, 문장이 사라지면 항목도 사라짐). 결과 변환기·`RETURNING` 검사·파라미터 변환은 매 호출. README 에 `bindparam()` 문장 재사용 안내 (#28).
- AI 코딩 에이전트용 안내 `django_sqlalchemy_bridge/llms.txt`(wheel 에 포함)와 같은 내용의 루트 `llms.txt`(llmstxt.org 형식, 영어): 언제 쓰고 언제 ORM 을 쓰는지, 지킬 것(`Engine`·`Session` 금지, signal·`save()` 우회, `bindparam` 재사용, `arun`, `INSERT … SELECT` 값의 타입), 공개 API 시그니처, 지원하지 않는 것, 예제. 모듈 docstring 이 이 파일과 읽는 명령을 가리킨다. 테스트가 두 파일의 일치, `__all__` 의 모든 이름과 시그니처, 빌드한 wheel 의 포함을 검사하고 `release_check.py --wheel` 을 `release.yml` 이 빌드 뒤에 돌린다 (#30).
- ORM 으로 쓰기 어려운 쿼리 예제 테스트(`tests/test_examples.py`, 상점 모델): 재귀 CTE, 여러 CTE, CTE 위 윈도 함수, `INSERT … ON CONFLICT DO UPDATE/DO NOTHING … RETURNING`, `INSERT … SELECT`(업서트 포함), `UPDATE … FROM`(테이블·집계 서브쿼리), PostgreSQL 전용 `LATERAL`·`DISTINCT ON`·`UPDATE … FROM (VALUES …)`·데이터를 바꾸는 CTE. 결과를 ORM·기대값과 대조하고 SQLite·PostgreSQL 에서 돈다(PostgreSQL 전용 문법만 SQLite 에서 건너뜀) (#30).

- `bulk_update(..., batch_size=None)`·`bulk_insert(..., batch_size=None)`: 양의 정수면 그 행 수마다 문장 하나(SQLite 는 `executemany()` 호출 하나)로 나눈다. 0 이하·정수가 아니면 실행 전 `ValueError`. 키 중복·누락 검사는 나누기 전에 전체로 하고, rowcount 는 합, `returning` 은 입력 순서. 기본값 `None`(PostgreSQL 한 문장)은 측정으로 정했다: 1,000,000행에서 `bulk_insert(batch_size=10_000)`은 클라이언트 tracemalloc 피크 849→226 MiB, 서버 백엔드 메모리 233→6 MiB 에 시간 4% 단축이지만 `bulk_update`는 배치하면 3~26% 느려진다(한 문장은 해시 조인 한 번, 작은 배치는 PK 인덱스 조회). README 에 언제 `batch_size`를 줄지 안내 (#35).
- 벤치 시나리오 `batch`: 200,000·1,000,000행 `bulk_insert`/`bulk_update` 를 `batch_size` 별로 재고, PostgreSQL 에서는 백엔드 프로세스의 `/proc/<pid>/status`(`RssAnon`·`VmHWM`)를 샘플링해 서버 메모리를 잰다. README "성능과 메모리" 7 (#35).
- `compare_table(check_db=True)`·`assert_table_matches(check_db=True)` 가 DB 컬럼 타입도 비교한다: 모델 필드의 `db_type()`(FK 는 대상 컬럼 타입)과 실제 컬럼 타입이 다르면 `Difference("db", "type", 컬럼, DB 타입, 모델이 바라는 타입)`. PostgreSQL 은 `format_type()` 표기로 맞춰 별칭(`varchar`·`serial`·`int4`·`timestamptz`·`decimal` 등)을 같게 보고 varchar 길이·numeric 정밀도까지 비교한다. SQLite 는 선언 타입 텍스트를 대소문자·공백만 맞춰 비교한다(decimal 정밀도는 선언에 없어 못 잡음). 그 밖의 백엔드는 타입을 비교하지 않는다. 테스트가 testapp·bench 의 모든 모델에서 차이 0개, 마이그레이션 누락(길이·타입·정밀도·FK 대상 타입)을 잡는 것을 SQLite·PostgreSQL 에서 고정한다 (#36).
- DRF·클래스 기반 뷰 레시피 문서 `docs/drf.md`: `ViewSet.list()`에서 매핑 행을 일반 `Serializer`로, `LimitOffsetPagination.paginate_queryset()`을 덮어 count 문장과 `limit`/`offset` 문장으로 페이지네이션, `validated_data`를 `INSERT … RETURNING`으로, ORM 쓰기와 브리지 읽기가 같은 트랜잭션, `TemplateView.get_context_data()`. 되지 않는 것(`ListView`·`ModelViewSet`·`ModelSerializer`·필터 백엔드·객체 권한·`CursorPagination`)과 대안. 코드는 `tests/test_recipes_drf.py`에서 SQLite·PostgreSQL·MySQL 로 돌고(`INSERT … RETURNING` 레시피는 MySQL 제외) 문서와 어긋나면 테스트가 실패한다. `djangorestframework`는 dev 의존성만 (#44).

### Changed

- README "설계 원칙"에 SQLAlchemy `Connection`·`Session`을 Django 커서를 감싼 DBAPI shim 위에서도 쓰지 않는 이유를 적음. 스파이크 판정은 기각: Django 트랜잭션은 보존되지만 SQLAlchemy 의 commit·rollback 이 조용히 무시되고, SQLAlchemy 타입 변환이 함정 D·E 를 되살리며, dialect 연결 훅이 Django 연결 상태를 바꾸고, Oracle 은 연결되지 않는다. 라이브러리 코드는 바뀌지 않음 (#39).
- 문장을 둘 이상 실행하는 `bulk_update`·`bulk_insert`(배치가 여럿이거나 SQLite `executemany()`)는 Django `bulk_create`처럼 `transaction.atomic(using, savepoint=False)` 안에서 돈다. 전에는 autocommit 의 SQLite 에서 중간 행이 제약을 어기면 앞 행이 남았다(4행 INSERT 의 4번째 행이 3번째와 unique 키가 겹치면 앞 3행이 남음). 그래서 바깥 `atomic()` 안에서 이 에러를 잡고 계속하려면 호출을 `atomic()`(savepoint)으로 감싸야 한다. 감싸지 않고 잡으면 바깥 트랜잭션이 롤백으로 표시된다(Django `bulk_create`와 같음) (#35).
- README "왜 만드나"를 "왜 쓰나"로 다시 씀: 목적은 표현력이고 성능은 대량 쓰기에서만 따라온다는 것, 언제 ORM 을 쓰는지, 예제 3개(재귀 CTE, 업서트 RETURNING, `UPDATE … FROM`). 예제 코드는 테스트 코드에서 `python -m tests.readme_examples --write` 로 옮기고 어긋나면 테스트가 실패한다. "AI 코딩 에이전트에게" 절에 `AGENTS.md`/`CLAUDE.md` 에 붙일 스니펫 (#30).
- `INSERT … SELECT` 의 SELECT 목록 값에는 대상 컬럼 타입을 직접 줘야 한다는 안내를 README "실행하기"·`llms.txt` 규칙 6에 근거(SQLAlchemy 가 `from_select()` 컬럼 이름을 공개 API 로 내주지 않아 브리지가 메우지 않음)와 타입이 없을 때의 증상(UUID·dict 는 드라이버 에러, SQLite aware datetime 은 오프셋 문자열로 저장)과 함께 적음. 타입을 준 `sa.literal()`·`sa.bindparam()` 이 ORM 과 같은 값을 저장하는지 테스트로 고정 (#34).

### Fixed

- 컬럼과 비교하는 값은 ORM 조회 `filter(field=v)` 의 경로(`get_prep_value()` 뒤 `get_db_prep_value(prepared=True)`, FK 는 대상 필드)로, INSERT `VALUES`·UPDATE `SET`·`bulk_*` 로 쓰는 값은 ORM 저장의 `get_db_prep_save()` 로 변환한다. 전에는 둘 다 `get_db_prep_value(prepared=False)` 라 Django 5.2 + MySQL 에서 `authors.c.id == 0` 이 `ValueError: The database backend does not accept 0 as a value for AutoField.` 였고(ORM `filter(id=0)` 은 실행), Django 6.1 에서는 반대로 `AutoField` 에 0 을 쓰는 INSERT 가 ORM 과 달리 검사 없이 나갔다. 비교 연산자의 직접 피연산자(`IN` 목록·`BETWEEN` 경계·튜플 항목 포함)가 조회 값이고, 컴파일러의 `visit_binary` 에서 가려 컴파일 캐시 항목에 bind 이름으로 둔다. `bulk_update` 의 `key` 값도 조회 값. 테스트가 Specimen 의 모든 필드에서 `==`·`<`·`IN` 파라미터를 ORM `filter()` 와 대조한다(SQLite·PostgreSQL·MySQL·Oracle) (#47).

## [0.1.0] - 2026-10-26

### Added

- `table_for(model)`: Django 모델에서 `sa.Table` 생성(공용 `metadata`, 모델별 캐시, `Column.info["django_field"]`). 지원하지 않는 컬럼 필드는 `UnsupportedFieldError`. `forget_table(model)` 로 캐시에서 제거.
- `compare_table()` / `assert_table_matches()`: 만든 테이블과 현재 모델(선택적으로 실제 DB 컬럼)의 어긋남을 `Difference` 리스트로 돌려주거나 assert.
- `execute(stmt, params=None, *, using="default")`: SQLAlchemy Core 문장을 Django 연결의 dialect 로 컴파일해 Django 커서로 실행. 행을 돌려주는 문장은 튜플 리스트, 그 밖은 rowcount. 매핑 시퀀스는 `executemany`. 값은 Django 필드 기준으로 변환(쓰기 `get_db_prep_value`, 읽기 ORM 변환기). 브리지 쓰기는 signal 을 발생시키지 않음.
- `execute_result()`: 같은 결과를 SQLAlchemy `Result` 로(옵트인). `Result` 생성은 비공개 API 라 `_sa_result.py` 한 곳에 둔다.
- `stream(stmt, params=None, *, chunk_size=2000, using="default")`: 행을 `chunk_size` 개씩 가져와 튜플로 내주는 제너레이터. `QuerySet.iterator()` 와 같은 `chunked_cursor()` 경로(PostgreSQL 서버 측 커서, `DISABLE_SERVER_SIDE_CURSORS` 존중)와 `fetchmany`. 중간에 멈춰도 커서를 닫는다. README 에 autocommit 의 `WITH HOLD` 동작과 PgBouncer 설정을 적음.
- `bulk_update(table, rows, *, key=None, columns=None)` / `bulk_insert(table, rows, *, returning=None)`: 대량 UPDATE·INSERT. PostgreSQL 은 컬럼마다 배열 파라미터 하나를 Django 컬럼 타입으로 캐스트한 `unnest()` 한 문장, 그 밖은 `executemany`(RETURNING 은 다중 VALUES 배치). 키·컬럼이 빠진 행, 중복 키, 빠뜨린 테이블 컬럼(자동 PK·`db_default` 제외)은 실행 전 `ValueError`. RETURNING 은 행 순서대로 ORM 변환기를 거친다.
- `arun(fn, /, *args, **kwargs)`: 동기 작업 단위(`execute`·`atomic()`·`on_commit`·ORM)를 `sync_to_async(thread_sensitive=True)` 한 번으로 실행. 반환값·예외를 그대로 전달하고, 코루틴 함수나 awaitable 을 돌려주는 함수는 `TypeError`. `fn` 앞뒤로 `atomic()` 밖 연결에 `close_old_connections()` 와 같은 정리(끊긴 연결·`CONN_MAX_AGE` 초과). 직접 의존성에 `asgiref>=3.8.1`.
- README 에 지원 필드 표와 사용 예.
- 벤치마크 `bench/`(`uv run --group bench python -m bench [시나리오...] --db sqlite|postgres`): PK 대량 UPDATE·대량 INSERT·대량 읽기(시간·tracemalloc·RSS)·PK 조회 오버헤드·async·CTE/윈도 리포트를 Django·django-pgbulk·django-cte·SQLAlchemy 단독과 비교. 결과는 `bench/results/*.json`(회차별 원값·버전·머신), README "성능과 메모리" 표는 `bench/readme.py` 가 결과 파일에서 생성하고 테스트가 일치를 검사. 비교 대상 라이브러리는 의존성 그룹 `bench` (#2).
- 테스트 하네스(`tests/` 아래 Django 테스트 프로젝트, SQLite·PostgreSQL 겸용)와 CI 매트릭스(Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1, PostgreSQL 18).

### Changed

- `RETURNING` 이 있는 SQLAlchemy DML(`execute`·`execute_result`·`stream`)과 `bulk_insert(returning=...)` 는 연결의 `can_return_columns_from_insert` 가 거짓이면(SQLite 3.35 미만) 실행 전에 `NotSupportedError`. `text()` 는 검사하지 않는다 (#12).
- `JSONField` 컬럼 타입이 `postgresql.JSONB().with_variant(sa.JSON(), "sqlite")` 로 바뀜. PostgreSQL 에서 `contains`·`contained_by`·`has_key`·`has_all`·`has_any`·`astext` 가 열리고 결과가 ORM 조회와 같다. `contains`·`contained_by` 의 우변은 `JSONField.get_db_prep_value()` 로 jsonb 바인딩. 전에는 `contains()` 가 `LIKE` 로 컴파일됐다. `compare_table` 은 `sa.JSON()` 컬럼을 타입 차이로 보고 (#12).
- README 에 "제약" 절: SQLAlchemy Python 측 `default`/`onupdate` 미적용(#19 에서 실행 전 거부로 바뀜), `standard_conforming_strings=off` 미지원, `bulk_insert` RETURNING 순서(키를 함께 RETURNING), `text()` 의 RETURNING 미검사 (#12).
- `table_for()` 컬럼 타입이 generic SQLAlchemy 타입을 감싼 `FieldType`(Django 필드를 들고 있는 `TypeDecorator`)으로 바뀜. `compare_table()` 은 감싼 타입끼리 비교한다.
- README 비동기 절: v0.1 은 `arun(fn)` 하나만 제공하고, SQLAlchemy 비동기 엔진·psycopg 비동기 직접 연결은 채택하지 않는다고 정정. `async with atomic()` 을 넣지 않는 이유를 재현 결과(같은 연결의 savepoint 중첩으로 정상 쪽이 예외 없이 롤백)에 맞게 구체화하고, Channels 에서 DB 작업이 스레드 하나로 줄 서는 점을 적음.

### Fixed

- 컬럼을 한 번 읽은(실행 포함) `insert(t)`/`update(t)`/`delete(t)` 에서 `.returning()` 을 파생한 문장이 RETURNING 없는 문장으로 취급되던 문제. `execute` 는 RETURNING 하한 검사를 건너뛰고 Django 변환 없이 원시 값(SQLite datetime 문자열·UUID hex, PostgreSQL jsonb 문자열)을 돌려줬고, `execute_result`·`stream` 은 "returns no rows" `ValueError` 였다. SQLAlchemy 가 DML 의 `exported_columns` 를 등록 없이 메모이즈해 파생 문장에 복사하기 때문이며(2.0·2.1 공통, 컴파일 결과의 `effective_returning` 도 같은 값), 이제 RETURNING 컬럼은 컴파일러가 렌더하는 컬럼에서 얻는다. INSERT 는 항상 `inline()` 으로 컴파일한다(명시적 RETURNING 이 있으면 결과가 같다) (#21).
- 직접 만든 `sa.Table` 의 SQLAlchemy Python 측 `Column(default=...)`/`Column(onupdate=...)`(값·callable) 컬럼을 비워 둔 INSERT·UPDATE 는 실행 전에 `ValueError`. 전에는 그 컬럼에 `NULL` 이 바인딩되어 nullable 컬럼에 조용히 `NULL` 이 저장되고 `onupdate` 컬럼은 UPDATE 마다 `NULL` 로 덮였다. 컴파일 결과의 공개 속성 `insert_prefetch`/`update_prefetch` 로 판정하며, 값을 직접 준 컬럼·SQL 식 default·`server_default`·Django `db_default` 는 영향 없음. README "제약" 절 갱신 (#19).
- JSON 키·인덱스·경로(`meta["k"]`, `meta[0]`, `meta[("a", "b")]`)를 dialect 표기로 바인딩. 전에는 SQLite 에서 `bad JSON path`, PostgreSQL 경로(`#>`)에서 오류였다 (#12).

## [0.0.1] - 2026-10-25

### Added

- Name reservation and design README. **Experimental** project; no public API.
