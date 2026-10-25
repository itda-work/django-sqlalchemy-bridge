# django-sqlalchemy-bridge AI Guide

> Django 연결·트랜잭션 안에서 SQLAlchemy 2.x Core 표현식을 쓰게 하는 라이브러리. **설계 단계(0.0.x)** — 공개 API 없음.

## 정본

| 무엇 | 정본 |
|------|------|
| 의존성, 지원 Python | `pyproject.toml` |
| 아키텍처 원칙 | `README.md` "Planned architecture" |
| 변경 이력 | `CHANGELOG.md` |

## 규약

- **Django 가 연결·트랜잭션 주인이다.** SQLAlchemy `Engine`/`Connection`/`Session` 을 실행 경로에 두지 않는다. 실행은 항상 Django cursor. (2026-10-25 사전 프로브: SA 가 Django DBAPI 연결을 쥐면 `atomic()` 롤백이 무시됐다.)
- **SQLAlchemy 공개 API 를 우선한다.** `_` 접두 내부 속성 사용 금지. 예외: 외부 행으로 SA `Result` 를 만드는 경로(공개 생성법 없음, 2026-10-25 스파이크 판정)는 어댑터 모듈 한 곳에 가두고 SA 상한을 핀해 CI 로 지킨다.
- 스키마·마이그레이션은 Django 모델이 단일 출처.
- 커밋 메시지는 영어 Conventional Commits. 문서는 README 포함 한국어(마스터 지시 2026-10-25). README 의 수치는 `bench/` 결과 파일이 있는 것만 싣는다.
- 작업 큐는 GitHub 이슈다. 결함은 재현한 뒤에 말하고, 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- 남의 저장소(aldjemy 포함)에는 이슈·코멘트·PR 을 만들지 않는다. 읽기만.
- 릴리스: `pyproject.toml` version 올림 → CHANGELOG 절 추가 → `python3 scripts/release_check.py` → `v<version>` 태그 푸시. `release.yml` 이 PyPI(trusted publishing, environment `pypi`)와 GitHub Release 에 올린다. 토큰은 저장하지 않는다.

## 테스트·CI 실행법

- 설치 `uv sync`. 린트 `uv run ruff check . && uv run ruff format --check .`.
- SQLite: `uv run pytest`. 백엔드는 `DSB_DB=sqlite|postgres` 로 고른다(미지정이면 sqlite, `REQUIRE_PG=1` 이면 postgres).
- PostgreSQL: 접속은 `DSB_PG_HOST/PORT/USER/PASSWORD/NAME`. `DSB_PG_HOST` 가 없으면 DB 테스트는 skip, `REQUIRE_PG=1` 이면 그 skip 이 실패다.
  ```sh
  docker run -d --name dsb-pg -e POSTGRES_USER=dsb -e POSTGRES_PASSWORD=dsb -e POSTGRES_DB=dsb -p 127.0.0.1:55432:5432 postgres:18
  DSB_PG_HOST=127.0.0.1 DSB_PG_PORT=55432 DSB_PG_USER=dsb DSB_PG_PASSWORD=dsb DSB_PG_NAME=dsb REQUIRE_PG=1 uv run pytest
  ```
- 테스트 모델을 바꾸면 `uv run django-admin makemigrations testapp --settings tests.settings --pythonpath .` (스모크 테스트가 누락을 잡는다).
- CI(`.github/workflows/ci.yml`)는 lint + Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1 매트릭스. 잡마다 PG 18 서비스로 SQLite·PG 를 각각 돈다. 매트릭스 버전은 `uv sync --locked` 뒤 `uv pip install` 로 덮어쓴다.
