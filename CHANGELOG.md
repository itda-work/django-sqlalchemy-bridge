# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `table_for(model)`: Django 모델에서 `sa.Table` 생성(공용 `metadata`, 모델별 캐시, `Column.info["django_field"]`). 지원하지 않는 컬럼 필드는 `UnsupportedFieldError`. `forget_table(model)` 로 캐시에서 제거.
- `compare_table()` / `assert_table_matches()`: 만든 테이블과 현재 모델(선택적으로 실제 DB 컬럼)의 어긋남을 `Difference` 리스트로 돌려주거나 assert.
- `execute(stmt, params=None, *, using="default")`: SQLAlchemy Core 문장을 Django 연결의 dialect 로 컴파일해 Django 커서로 실행. 행을 돌려주는 문장은 튜플 리스트, 그 밖은 rowcount. 매핑 시퀀스는 `executemany`. 값은 Django 필드 기준으로 변환(쓰기 `get_db_prep_value`, 읽기 ORM 변환기). 브리지 쓰기는 signal 을 발생시키지 않음.
- `execute_result()`: 같은 결과를 SQLAlchemy `Result` 로(옵트인). `Result` 생성은 비공개 API 라 `_sa_result.py` 한 곳에 둔다.
- README 에 지원 필드 표와 사용 예.
- 테스트 하네스(`tests/` 아래 Django 테스트 프로젝트, SQLite·PostgreSQL 겸용)와 CI 매트릭스(Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1, PostgreSQL 18).

### Changed

- `table_for()` 컬럼 타입이 generic SQLAlchemy 타입을 감싼 `FieldType`(Django 필드를 들고 있는 `TypeDecorator`)으로 바뀜. `compare_table()` 은 감싼 타입끼리 비교한다.
- README 비동기 절: v0.1 은 `arun(fn)` 하나만 제공하고, SQLAlchemy 비동기 엔진·psycopg 비동기 직접 연결은 채택하지 않는다고 정정.

## [0.0.1] - 2026-10-25

### Added

- Name reservation and design README. **Experimental** project; no public API.
