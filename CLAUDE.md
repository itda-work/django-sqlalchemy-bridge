# django-sqlalchemy-bridge AI Guide

> Django 연결·트랜잭션 안에서 SQLAlchemy 2.x Core 표현식을 쓰게 하는 라이브러리. **0.1.0 (알파, 2026-10-26 릴리스)** — API: `table_for`·`execute`·`stream`·`bulk_*`·`arun` (+Unreleased `execute_pipeline`·`raw`·`copy_to`). v0.1 이후 후보는 #17. Unreleased: 컴파일 캐시(#28), MySQL(#37), Oracle(#38), `execute_pipeline`(#50), `bulk_insert(method="copy")`(#54), `raw`(#57), `copy_to`(#62).

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
- 커밋 메시지는 영어 Conventional Commits. 문서는 README 포함 한국어(마스터 지시 2026-10-25). 예외: `llms.txt`(패키지 정본·루트 사본)와 README 의 에이전트 스니펫은 영어(마스터 지시 2026-10-26, 전 세계 사용자의 에이전트가 읽는다). README 의 수치는 `bench/` 결과 파일이 있는 것만 싣는다.
- 작업 큐는 GitHub 이슈다. 결함은 재현한 뒤에 말하고, 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- 남의 저장소(aldjemy 포함)에는 이슈·코멘트·PR 을 만들지 않는다. 읽기만.
- 릴리스: `pyproject.toml` version 올림 → CHANGELOG 절 추가 → `python3 scripts/release_check.py` → `v<version>` 태그 푸시. `release.yml` 이 PyPI(trusted publishing, environment `pypi`)와 GitHub Release 에 올린다. 토큰은 저장하지 않는다.

## 테스트·CI 실행법

- 설치 `uv sync`. 린트 `uv run ruff check . && uv run ruff format --check .`.
- SQLite: `uv run pytest`. 백엔드는 `DSB_DB=sqlite|postgres|mysql|oracle` 로 고른다(미지정이면 sqlite, `REQUIRE_PG=1` 이면 postgres, `REQUIRE_MYSQL=1` 이면 mysql, `REQUIRE_ORACLE=1` 이면 oracle).
- PostgreSQL: 접속은 `DSB_PG_HOST/PORT/USER/PASSWORD/NAME`. `DSB_PG_HOST` 가 없으면 DB 테스트는 skip, `REQUIRE_PG=1` 이면 그 skip 이 실패다.
  ```sh
  docker run -d --name dsb-pg -e POSTGRES_USER=dsb -e POSTGRES_PASSWORD=dsb -e POSTGRES_DB=dsb -p 127.0.0.1:55432:5432 postgres:18
  DSB_PG_HOST=127.0.0.1 DSB_PG_PORT=55432 DSB_PG_USER=dsb DSB_PG_PASSWORD=dsb DSB_PG_NAME=dsb REQUIRE_PG=1 uv run pytest
  ```
- PostgreSQL 서버측 바인딩: `DSB_PG_SERVER_SIDE_BINDING=1` 이면 `OPTIONS={"server_side_binding": True, "prepare_threshold": 5}`(값이 프로토콜 파라미터로 가고 `ServerBindingCursor`). 같은 연결에서 같은 문장이 5번째 실행부터 서버 prepared statement 가 되므로 스위트가 prepared 경로도 지난다. CI 는 잡마다 PG 단계 뒤에 이 설정으로 한 번 더 돈다.
- MySQL 8.4: 접속은 `DSB_MYSQL_HOST/PORT/USER/PASSWORD/NAME`(HOST 는 `127.0.0.1`, `localhost` 는 소켓으로 간다). 없으면 skip, `REQUIRE_MYSQL=1` 이면 실패. 드라이버 `mysqlclient` 는 C 확장이라 `dev` 가 아닌 `mysql` 그룹이다(`uv sync --group mysql`, 빌드에 MySQL/MariaDB 클라이언트 라이브러리와 `pkg-config` 필요. 호스트에 brew 로 새로 깔지 말고 없으면 Linux 컨테이너나 CI 로). 테스트 DB 를 만들어야 하므로 root 로 붙는다.
  ```sh
  docker run -d --name dsb-mysql -e MYSQL_ROOT_PASSWORD=dsb -e MYSQL_DATABASE=dsb -p 127.0.0.1:53306:3306 mysql:8.4
  uv sync --group mysql
  DSB_DB=mysql DSB_MYSQL_HOST=127.0.0.1 DSB_MYSQL_PORT=53306 DSB_MYSQL_USER=root DSB_MYSQL_PASSWORD=dsb DSB_MYSQL_NAME=dsb REQUIRE_MYSQL=1 uv run --no-sync pytest
  ```
- Oracle 23ai Free: 접속은 `DSB_ORACLE_HOST/PORT/USER/PASSWORD/NAME`(NAME 은 서비스 이름 `FREEPDB1`, Easy Connect 로 붙는다). 없으면 skip, `REQUIRE_ORACLE=1` 이면 실패. Django 가 테스트 사용자(`test_system`)를 만들므로 `system` 으로 붙는다. 드라이버 `oracledb`(thin, 순수 파이썬)는 `dev` 그룹. 무료 19c 이미지가 없어 검증 버전은 23ai 뿐이다.
  ```sh
  docker run -d --name dsb-oracle -e ORACLE_PASSWORD=dsb -p 127.0.0.1:51521:1521 gvenzl/oracle-free:23-slim-faststart
  DSB_DB=oracle DSB_ORACLE_HOST=127.0.0.1 DSB_ORACLE_PORT=51521 DSB_ORACLE_USER=system DSB_ORACLE_PASSWORD=dsb DSB_ORACLE_NAME=FREEPDB1 REQUIRE_ORACLE=1 uv run --no-sync pytest
  ```
- 테스트 모델을 바꾸면 `uv run django-admin makemigrations testapp --settings tests.settings --pythonpath .` (스모크 테스트가 누락을 잡는다).
- CI(`.github/workflows/ci.yml`)는 lint + Python 3.12/3.14 × Django 5.2/6.1 × SQLAlchemy 2.0/2.1 매트릭스. 잡마다 PG 18·MySQL 8.4·Oracle 23ai Free 서비스로 SQLite·PG·PG(서버측 바인딩)·MySQL·Oracle 을 각각 돈다(MariaDB 는 CI 에 없다). 매트릭스 버전은 `uv sync --locked` 뒤 `uv pip install` 로 덮어쓴다.

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
- 구현 중 발견(#12): SA 비교자는 dialect 를 고르기 전 식의 기본 타입에서 나온다. `JSON().with_variant(JSONB(), "postgresql")` 는 `contains()` 가 조용히 `LIKE` 가 되므로 `JSONB().with_variant(JSON(), "sqlite")` 로 둔다. JSON 키·경로 bind(`JSONIndexType`·`JSONPathType`)는 값이 아니라 문법이라 SA dialect bind processor 를 쓴다(안 쓰면 SQLite `bad JSON path`). RETURNING 하한은 Django 5.2·6.1 공통 feature `can_return_columns_from_insert`(`can_return_rows_from_update` 는 5.2 에 없음).
- 구현 중 발견(#21): SA DML 의 `exported_columns`·`_all_selected_columns` 는 `HasMemoized` 에 등록되지 않는 `ro_memoized_property` 라 `_generate()` 가 파생 문장에 복사한다 — 한 번 읽은(실행한) `insert(t)` 에서 `.returning()` 을 파생하면 컬럼이 비어 보인다. `compiled.effective_returning` 도 같은 메모를 읽는다. RETURNING 컬럼은 dialect 컴파일러의 `returning_clause` 가 받는 컬럼으로 얻는다(`_ReturningCompiler`). SELECT 의 `selected_columns` 는 등록된 메모라 안전.
- 구현 중 발견(#28): `Compiled.statement` 가 컴파일한 문장을 붙잡으므로 문장을 weak key 로 하는 캐시는 생성형 복사본(`inline()`·인자 없는 `execution_options()`)을 컴파일해야 항목이 풀린다. 결과 컨버터는 `connection.ops` bound method 라(SQLite 는 `self.connection.timezone` 을 호출 때 읽음) vendor·alias 를 넘어 캐시하지 않는다 — #60 부터 연결 객체(`connections[alias]`, 스레드마다 하나)의 속성에 `WeakKeyDictionary[FieldType, …]` 로 둔다. 모듈 전역 `WeakKeyDictionary[연결, …]` 은 값(bound method → `ops` → 연결)이 키를 붙잡아 연결이 해제되지 않는다(`test_the_converters_do_not_keep_their_connection_alive`). `django.db.connection` 프록시는 `__eq__` 가 있어 해시가 안 되지만 `getattr`·`setattr` 이 그 스레드의 연결로 간다.
- 구현 중 발견(#37): Django vendor `"mysql"` 은 MySQL·MariaDB 둘 다라 dialect 키를 `mysql_is_mariadb`·`sql_mode`(`NO_BACKSLASH_ESCAPES`)로 더 가른다(안 가르면 리터럴 백슬래시가 조용히 두 개). MariaDB 는 INSERT·DELETE RETURNING 만 있고 UPDATE 는 없다(Django feature 는 insert 하나뿐이라 문장 종류로 판정). MySQL 은 JSON 과 문자열을 비교하면 문자열을 JSON 문자열로 봐 `meta["k"] == 1` 이 `{"k": "1"}` 에 맞는다 — JSON 바인드를 ORM 처럼 `JSON_EXTRACT(%s, '$')` 로 감싼다(`_MySQLJSON.bind_expression`). MySQL 은 DDL 이 즉시 커밋돼 테스트 롤백이 스키마를 되돌리지 못한다 — DDL 테스트는 `undo_ddl` 픽스처로 테이블을 다시 만든다. mysqlclient 는 untyped aware datetime 의 오프셋을 버린다.
- 구현 중 발견(#38): Oracle 은 `RETURNING … INTO` 출력 바인드로 행을 주는데 Django 커서로는 못 받아, 실행하면 에러 없이 rowcount 가 온다 — vendor `"oracle"` 이면 `check_returning` 이 모두 거부한다(Django feature 는 참). Django `quote_name()` 은 Oracle 식별자를 대문자·30자(해시)로 바꾸고 SA Oracle 은 대소문자 섞인·긴 이름·예약어(`uid`, `level`)를 그대로 따옴표로 감싼다 — 브리지 Oracle dialect 의 `IdentifierPreparer.quote()` 가 Django 철자로 쓰고(`quoted_name(quote=True)` 도. `quoted_name.upper()` 는 자기 자신을 돌려주므로 `str()` 먼저), 결과 키는 문장 컬럼 이름으로 되돌린다(`_result_keys`, 다른 vendor 는 그대로). Oracle 은 `NCLOB` 을 `=` 로 비교 못 해(`ORA-22848`) JSON `==`/`!=` 를 컴파일러 `visit_binary` 에서 `JSON_EQUAL()` 로 쓴다(비교자는 기본 타입에서 나오므로 타입 variant 로는 안 된다). SA 2.1 Oracle 의 `as_*()` 는 `JSON_VALUE(... ERROR ON ERROR)` 라 키 없는 행에 문장이 실패해 접미사를 뗀다. SA 2.0 Oracle 에는 JSON 이 없고 `construct_expanded_state` 가 모든 bind processor 를 만들어 JSON 에서 깨진다 — `FieldType.bind_processor` 는 `None`, Oracle JSON variant 는 SA 2.1+. Django Oracle 커서는 정밀도 없는 숫자(`sum()`)를 텍스트로 `int`/`Decimal` 을 고르므로 ORM 처럼 `Expression(output_field=field).convert_value` 를 끼운다. SA 는 연결하지 않은 Oracle dialect 로 다중 행 `VALUES` 를 컴파일하지 않는다. Oracle 도 DDL 이 즉시 커밋돼 `undo_ddl` 이 다시 만들고, `CASCADE CONSTRAINTS` 가 FK 를 함께 지우므로 가리키는 테이블도 다시 만든다. 무인용 `uid` 는 Oracle 의 UID 의사 컬럼이다.
- 구현 중 발견(#47): ORM 은 조회 값(`get_prep_value` → `get_db_prep_value(prepared=True)`)과 쓰기 값(`get_db_prep_save`)을 따로 준비하고, 백엔드의 쓰기 검사가 버전마다 다른 쪽에 있다(Django 5.2 MySQL `validate_autopk_value` 는 `get_db_prep_value(prepared=False)`, 6.1 은 `get_db_prep_save`). 브리지는 비교 연산자의 직접 피연산자 bind 를 컴파일러 `visit_binary` 에서 조회 값으로 가른다(Oracle JSON `JSON_EQUAL` 분기도 기록). Django 5.2 는 `filter(flag=True)` 를 파라미터 없는 `WHERE flag` 로 쓴다.
- 구현 중 발견(#50): psycopg 파이프라인 안에서는 Sync 전까지 커서의 `description`·`rowcount` 가 비어 있다 — 모두 보낸 뒤 `pipeline.sync()` 하고 읽는다. 실패 항목은 결과(`pgresult`)가 없는 첫 커서인데 `executemany(returning=False)` 는 `pgresult` 를 남기지 않아 뒤에 Sync 가 필요하고, 트랜잭션 밖의 Sync 는 커밋이라 문장이 둘 이상이면 `atomic(savepoint=False)` 를 연다. `PipelineAborted` 는 첫 예외에서 멈추면 노출되지 않는다(psycopg 가 나머지를 버리고 블록 종료 에러를 삼킴). sync 에서 나는 예외는 psycopg 원본이라 `connection.wrap_database_errors` 로 감싼다.
- 구현 중 발견(#54): psycopg `Cursor.copy()` 는 Django `CursorWrapper.execute()` 를 지나지 않아 `execute_wrapper`·`validate_no_broken_transaction()`·`wrap_database_errors` 가 모두 빠진다 — 브리지가 직접 부른다. Django PG 의 `CursorDebugWrapper.copy()` 는 COPY 에 들어갈 때 기록해(행을 보내기 전) 시간이 0 에 가깝다 — 브리지는 래퍼 아래 `cursor.cursor.copy()` 를 `debug_sql()` 로 감싸 행을 다 보낸 뒤까지 잰다. COPY 뒤 psycopg `rowcount` 는 넣은 행 수다.
- 구현 중 발견(#59): SQLite 는 컬럼(NUMERIC 친화성)과 텍스트를 숫자로 비교하지만 `SUM()`·산술·`CASE` 결과는 친화성이 없어 숫자 < 텍스트로 늘 거짓이다 — Django 가 `DecimalField` 값을 텍스트로 묶으므로 `sum(price) >= x` 가 에러 없이 0행. ORM 은 출력 필드가 `DecimalField` 인 `Func`·`CombinedExpression`·`Case`·`Window` 를 `(CAST(... AS NUMERIC))` 으로 감싼다(`SQLiteNumericMixin`, `CAST` 는 그 타입의 친화성). 브리지 SQLite 컴파일러(`_SQLiteNumericCompiler`)가 타입이 DecimalField `FieldType` 인 function·funcfilter·over·withingroup·case·binary·neg 를 감싸고, 윈도·`FILTER` 안의 함수는 바깥에서(Django `Window.as_sqlite` 와 같음). SA `func.avg()` 는 `NullType` 이라 `type_=` 없이는 감싸지도 Decimal 로 읽지도 않는다.
- 구현 중 발견(#62): psycopg 3 `Cursor.copy(sql, params)` 는 `ClientCursor`·`ServerBindingCursor`(Django `OPTIONS["server_side_binding"]`) 모두 params 를 클라이언트에서 SQL 에 끼워 넣어 `COPY (SELECT … %s) TO STDOUT` 이 된다(이슈의 "서버측 커서는 실패" 예상과 다름, psycopg 3.3.6). 다만 params 가 비면(`None`·`()`) 끼워 넣기를 건너뛰어 `%%` 가 그대로 간다(함정 B) — 브리지는 `connection.Database.ClientCursor(connection.connection).mogrify(sql, params)` 로 리터럴 SQL 을 만든 뒤 params 없이 COPY 한다(빈 튜플이어도 `%%`→`%`). COPY TO 데이터는 psycopg 가 행 하나씩(`memoryview`) 준다. `Copy` 블록을 예외로 나오면 psycopg 가 서버에 cancel 을 보내 나머지를 버리는데, 트랜잭션 안이면 그 cancel 이 트랜잭션을 깬다(결과가 커서 서버가 아직 보내는 중일 때만, 작은 결과는 무사 — 크기에 따라 갈림) — `copy_to` 는 `GeneratorExit` 에서 autocommit 이 아니면 남은 데이터를 읽어 버린다. Django PG 는 세션 `TimeZone` 을 `USE_TZ` 면 UTC 로 두어 COPY 의 timestamptz 는 `+00` 이다.
