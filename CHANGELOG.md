# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `table_for(model)`: Django 모델에서 `sa.Table` 생성(공용 `metadata`, 모델별 캐시, `Column.info["django_field"]`). 지원하지 않는 컬럼 필드는 `UnsupportedFieldError`. `forget_table(model)` 로 캐시에서 제거.
- `compare_table()` / `assert_table_matches()`: 만든 테이블과 현재 모델(선택적으로 실제 DB 컬럼)의 어긋남을 `Difference` 리스트로 돌려주거나 assert.
- README 에 지원 필드 표와 사용 예.
- 테스트 하네스(`tests/` 아래 Django 테스트 프로젝트, SQLite·PostgreSQL 겸용)와 CI 매트릭스(Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1, PostgreSQL 18).

### Changed

- README 비동기 절: v0.1 은 `arun(fn)` 하나만 제공하고, SQLAlchemy 비동기 엔진·psycopg 비동기 직접 연결은 채택하지 않는다고 정정.

## [0.0.1] - 2026-10-25

### Added

- Name reservation and design README. **Experimental** project; no public API.
