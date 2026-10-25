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
- README 에 지원 필드 표와 사용 예.
- 테스트 하네스(`tests/` 아래 Django 테스트 프로젝트, SQLite·PostgreSQL 겸용)와 CI 매트릭스(Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1, PostgreSQL 18).

### Changed

- `table_for()` 컬럼 타입이 generic SQLAlchemy 타입을 감싼 `FieldType`(Django 필드를 들고 있는 `TypeDecorator`)으로 바뀜. `compare_table()` 은 감싼 타입끼리 비교한다.
- README 비동기 절: v0.1 은 `arun(fn)` 하나만 제공하고, SQLAlchemy 비동기 엔진·psycopg 비동기 직접 연결은 채택하지 않는다고 정정.

## [0.0.1] - 2026-10-25

### Added

- Name reservation and design README. **Experimental** project; no public API.
