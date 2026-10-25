# django-sqlalchemy-bridge AI Guide

> Django 연결·트랜잭션 안에서 SQLAlchemy 2.x Core 표현식을 쓰게 하는 라이브러리. **0.0.x, 미릴리스** — v0.1 API(`table_for`·`execute`·`stream`·`bulk_*`·`arun`)가 `main` 에 있다(2026-10-25 기준, #3~#8).

## 정본

| 무엇 | 정본 |
|------|------|
| 의존성, 지원 Python | `pyproject.toml` |
| 아키텍처 원칙 | `README.md` "설계 원칙" |
| 변경 이력 | `CHANGELOG.md` |
| 1차 스파이크 판정 원문 | `~/.hermes/kanban/logs/t_9603d207.log` 의 `VERDICT.md` (저장소 밖, 2026-10-25) |

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

## 작업 방식

- **이슈 먼저.** 브랜치 `issue-<N>-<slug>`, PR 본문 `Closes #N`, squash 병합 제목 끝에 `(#N) (#PR)`. 검증 결과는 이슈 코멘트로 남긴다.
- **메인 세션은 오케스트레이터다.** 이슈·브리프 작성, 위임, 검증(테스트·CI 직접 실행, 경계 프로브), 이슈 기록, 병합을 맡고 구현 코드는 쓰지 않는다.
- **구현**: herdr pane 의 `cc-alt --model claude-opus-5-5 --effort medium` (셸 pane 에서 `herdr pane run`, 뒤 `herdr agent rename <id> dsb-impl`). 이슈마다 pane 을 새로 띄우고 병합하면 닫는다.
- 브리프·보고는 `.work/issue-<N>/`(git 무시)로 주고받는다. 병합 후 결론이 이슈 코멘트에 있는지 확인하고 `rm -rf -- ".work/issue-${N:?}"` 로 지운다. 와일드카드 `rm` 은 쓰지 않는다.

## 함정 (1차 스파이크 판정 ★A~E, 모두 테스트로 고정)

- **A. 외부 행으로 SA `Result` 를 만드는 공개 생성법이 없다.** `_sa_result.py` 한 곳에 가두고 SA `<2.2` 핀, CI 2.0·2.1 (`test_execute_result_*`).
- **B. format paramstyle 에서 SA 는 리터럴 `%` 를 `%%` 로 쓴다. Django 커서는 params=None 이면 되돌리지 않아 에러 없이 결과가 틀린다.** 파라미터가 없어도 항상 빈 튜플을 넘긴다 (`test_literal_percent_signs_survive`).
- **C. SA bind processor 의 공개 경로(`ExpandedState.processors`)에는 expanding IN 만 들어 있다**(`compiled._bind_processors` 는 비공개). 브리지는 SA 타입 처리를 쓰지 않고 `FieldType.field.get_db_prep_value()` 로 변환한다.
- **D. SQLite 에서 `sa.DateTime(timezone=True)` bind 는 오프셋을 버려 USE_TZ 값이 9시간 밀린다(에러 없음).** Django 필드 변환 (`test_aware_datetime_is_stored_in_utc_and_read_back_aware`).
- **E. `sa.Enum(TextChoices)` 는 기본으로 멤버 이름을 저장한다(Django 는 값).** `table_for` 는 choices 필드를 바탕 타입으로 매핑한다 (`test_text_choices_are_stored_by_value`).
- 그 밖: SA 가 Django DBAPI 연결을 쥐면 close 시 rollback 으로 `atomic()` 이 깨진다(방식 2 폐기). Django 의 `get_db_converters` 계열은 문서에 없는 내부 API 라 Django 매트릭스 CI 로 지킨다. aldjemy 는 SA 가 아니라 Django 6.0(M2M `concrete=False`)에서 깨진다.
- 구현 중 발견(#5·#6·#7): psycopg dialect 의 bind cast(`%s::JSON`)는 `BindTyping.NONE` 으로 끈다. PG 단일 INSERT 의 암묵적 `RETURNING id` 는 `Insert.inline()` 으로 끈다. SA 2.0 PG 컴파일러는 연결 전 리터럴 백슬래시를 두 번 쓴다. PG `DECLARE` 는 DML RETURNING·데이터 변경 CTE 를 못 받는다. `unnest` 캐스트는 `varchar(n)[]` 가 값을 조용히 자르므로 타입 수식어를 뗀다.
