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
- **SQLAlchemy 공개 API 만 쓴다.** `_` 접두 내부 속성 사용 금지.
- 스키마·마이그레이션은 Django 모델이 단일 출처.
- 커밋 메시지는 영어 Conventional Commits. 문서는 한국어(README 는 영어).
- 작업 큐는 GitHub 이슈다. 결함은 재현한 뒤에 말하고, 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- 남의 저장소(aldjemy 포함)에는 이슈·코멘트·PR 을 만들지 않는다. 읽기만.
- 릴리스: `pyproject.toml` version 올림 → CHANGELOG 절 추가 → `python3 scripts/release_check.py` → `v<version>` 태그 푸시. `release.yml` 이 PyPI(trusted publishing, environment `pypi`)와 GitHub Release 에 올린다. 토큰은 저장하지 않는다.
