# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `table_for(model)`: Django 모델에서 `sa.Table` 생성(공용 `metadata`, 모델별 캐시, `Column.info["django_field"]`). 지원하지 않는 컬럼 필드는 `UnsupportedFieldError`. `forget_table(model)` 로 캐시에서 제거.
- `compare_table()` / `assert_table_matches()`: 만든 테이블과 현재 모델(선택적으로 실제 DB 컬럼)의 어긋남을 `Difference` 리스트로 돌려주거나 assert.
- `execute(stmt, params=None, *, using="default")`: SQLAlchemy Core 문장을 Django 연결의 dialect 로 컴파일해 Django 커서로 실행. 행을 돌려주는 문장은 튜플 리스트, 그 밖은 rowcount. 매핑 시퀀스는 `executemany`. 값은 Django 필드 기준으로 변환(쓰기 `get_db_prep_value`, 읽기 ORM 변환기). 브리지 쓰기는 signal 을 발생시키지 않음.
- `execute_result()`: 같은 결과를 SQLAlchemy `Result` 로(옵트인). `Result` 생성은 비공개 API 라 `_sa_result.py` 한 곳에 둔다.
- `stream(stmt, params=None, *, chunk_size=2000, using="default")`: 행을 `chunk_size` 개씩 가져와 튜플로 내주는 제너레이터. `QuerySet.iterator()` 와 같은 `chunked_cursor()` 경로(PostgreSQL 서버 측 커서, `DISABLE_SERVER_SIDE_CURSORS` 존중)와 `fetchmany`. 중간에 멈춰도 커서를 닫는다. README 에 autocommit 의 `WITH HOLD` 동작과 PgBouncer 설정을 적음.
- `bulk_update(table, rows, *, key=None, columns=None)` / `bulk_insert(table, rows, *, returning=None)`: 대량 UPDATE·INSERT. PostgreSQL 은 컬럼마다 배열 파라미터 하나를 Django 컬럼 타입으로 캐스트한 `unnest()` 한 문장, 그 밖은 `executemany`(RETURNING 은 다중 VALUES 배치). 키·컬럼이 빠진 행, 중복 키, 빠뜨린 테이블 컬럼(자동 PK·`db_default` 제외)은 실행 전 `ValueError`. RETURNING 은 행 순서대로 ORM 변환기를 거친다.
- `arun(fn, /, *args, **kwargs)`: 동기 작업 단위(`execute`·`atomic()`·`on_commit`·ORM)를 `sync_to_async(thread_sensitive=True)` 한 번으로 실행. 반환값·예외를 그대로 전달하고, 코루틴 함수나 awaitable 을 돌려주는 함수는 `TypeError`. `fn` 앞뒤로 `atomic()` 밖 연결에 `close_old_connections()` 와 같은 정리(끊긴 연결·`CONN_MAX_AGE` 초과). 직접 의존성에 `asgiref>=3.8.1`.
- README 에 지원 필드 표와 사용 예.
- 테스트 하네스(`tests/` 아래 Django 테스트 프로젝트, SQLite·PostgreSQL 겸용)와 CI 매트릭스(Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1, PostgreSQL 18).

### Changed

- `RETURNING` 이 있는 SQLAlchemy DML(`execute`·`execute_result`·`stream`)과 `bulk_insert(returning=...)` 는 연결의 `can_return_columns_from_insert` 가 거짓이면(SQLite 3.35 미만) 실행 전에 `NotSupportedError`. `text()` 는 검사하지 않는다 (#12).
- `JSONField` 컬럼 타입이 `postgresql.JSONB().with_variant(sa.JSON(), "sqlite")` 로 바뀜. PostgreSQL 에서 `contains`·`contained_by`·`has_key`·`has_all`·`has_any`·`astext` 가 열리고 결과가 ORM 조회와 같다. `contains`·`contained_by` 의 우변은 `JSONField.get_db_prep_value()` 로 jsonb 바인딩. 전에는 `contains()` 가 `LIKE` 로 컴파일됐다. `compare_table` 은 `sa.JSON()` 컬럼을 타입 차이로 보고 (#12).
- README 에 "제약" 절: SQLAlchemy Python 측 `default`/`onupdate` 미적용(#19 에서 실행 전 거부로 바뀜), `standard_conforming_strings=off` 미지원, `bulk_insert` RETURNING 순서(키를 함께 RETURNING), `text()` 의 RETURNING 미검사 (#12).

### Fixed

- 컬럼을 한 번 읽은(실행 포함) `insert(t)`/`update(t)`/`delete(t)` 에서 `.returning()` 을 파생한 문장이 RETURNING 없는 문장으로 취급되던 문제. `execute` 는 RETURNING 하한 검사를 건너뛰고 Django 변환 없이 원시 값(SQLite datetime 문자열·UUID hex, PostgreSQL jsonb 문자열)을 돌려줬고, `execute_result`·`stream` 은 "returns no rows" `ValueError` 였다. SQLAlchemy 가 DML 의 `exported_columns` 를 등록 없이 메모이즈해 파생 문장에 복사하기 때문이며(2.0·2.1 공통, 컴파일 결과의 `effective_returning` 도 같은 값), 이제 RETURNING 컬럼은 컴파일러가 렌더하는 컬럼에서 얻는다. INSERT 는 항상 `inline()` 으로 컴파일한다(명시적 RETURNING 이 있으면 결과가 같다) (#21).
- 직접 만든 `sa.Table` 의 SQLAlchemy Python 측 `Column(default=...)`/`Column(onupdate=...)`(값·callable) 컬럼을 비워 둔 INSERT·UPDATE 는 실행 전에 `ValueError`. 전에는 그 컬럼에 `NULL` 이 바인딩되어 nullable 컬럼에 조용히 `NULL` 이 저장되고 `onupdate` 컬럼은 UPDATE 마다 `NULL` 로 덮였다. 컴파일 결과의 공개 속성 `insert_prefetch`/`update_prefetch` 로 판정하며, 값을 직접 준 컬럼·SQL 식 default·`server_default`·Django `db_default` 는 영향 없음. README "제약" 절 갱신 (#19).
- JSON 키·인덱스·경로(`meta["k"]`, `meta[0]`, `meta[("a", "b")]`)를 dialect 표기로 바인딩. 전에는 SQLite 에서 `bad JSON path`, PostgreSQL 경로(`#>`)에서 오류였다 (#12).

- `table_for()` 컬럼 타입이 generic SQLAlchemy 타입을 감싼 `FieldType`(Django 필드를 들고 있는 `TypeDecorator`)으로 바뀜. `compare_table()` 은 감싼 타입끼리 비교한다.
- README 비동기 절: v0.1 은 `arun(fn)` 하나만 제공하고, SQLAlchemy 비동기 엔진·psycopg 비동기 직접 연결은 채택하지 않는다고 정정. `async with atomic()` 을 넣지 않는 이유를 재현 결과(같은 연결의 savepoint 중첩으로 정상 쪽이 예외 없이 롤백)에 맞게 구체화하고, Channels 에서 DB 작업이 스레드 하나로 줄 서는 점을 적음.

## [0.0.1] - 2026-10-25

### Added

- Name reservation and design README. **Experimental** project; no public API.
