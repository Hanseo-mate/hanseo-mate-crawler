# 한서대학교 학사일정 크롤러 연동

Spring `feature/academic-calendar`의 `docs/academic-calendar-integration.md`와
`docs/0005_academic_calendar.sql`을 읽고 구현했다. 문서의 기준 main 커밋은
`ec1548e`이며, 해당 기능이 main에 병합되었다고 가정하지 않는다.

## API

기존 `api_main:app`에 아래 경로를 추가했다.

```http
POST /academic-calendar-crawl/run
Content-Type: application/json

{"academic_year":2026,"semesters":[1,2],"mode":"background"}
```

접수 이력을 DB에 저장하고 별도 스레드를 시작한 뒤 HTTP **202**를 반환한다.
학교 HTTP 요청과 파싱·저장은 요청 처리 스레드에서 기다리지 않는다.

```json
{"status":"starting","run_id":"32자리 UUID hex 문자열"}
```

- Spring이 요구하는 `status=starting`, 비어 있지 않은 `run_id`와 호환된다.
- `academic_year`: 정수 2026~9998. 다음 해 날짜의 표현 범위와 Spring 공개 API 상한을 맞췄다.
- `semesters`: `[1]`, `[2]`, `[1,2]`, `[2,1]`. 빈 목록·중복·다른 숫자는 거절한다.
- 숫자 문자열·실수·불리언을 정수로 변환하지 않는다. 알 수 없는 요청 필드도 거절한다.
- `mode`: `background`만 지원하며 생략해도 같은 값이다. `sync` 등은 422다.
- 이미 진행 중인 학사일정 실행: **409**. 잘못된 요청: **422**.
- 이력 DB 접속/스키마 오류 또는 작업 시작 실패: **503**. 접수 불가능한 상태를 성공으로 응답하지 않는다.
- 수집은 요청한 연도와 학기만 수행한다. 다른 연도나 다음 학년도 페이지를 추가 조회하지 않는다.

실행 상태는 메모리가 아닌 DB에서 조회한다. 재시작 후에도 같은 `run_id`로 확인할 수 있다.

```http
GET /academic-calendar-crawl/status?run_id=<접수 응답의 run_id>
GET /academic-calendar-crawl/status
```

ID를 생략하면 최신 실행, 이력이 없으면 `{"status":"idle","run_id":null}`이다.
존재하지 않는 ID는 404다. 상태 조회도 DB 장애 시 503을 반환한다.

반환 필드:

| 범위 | 필드 |
|---|---|
| 실행 | run_id, academic_year, semesters, status, started_at, finished_at, error |
| results의 `"1"`/`"2"` | academic_year, semester, status, source_url, event_count, inserted, updated, unchanged, deactivated, started_at, finished_at, error, preservation_reason |

학기 상태는 `pending`, `running`, `completed`, `unpublished`, `failed`다.
실행 상태는 `starting`, `running`, `completed`, `unpublished`, `partial_unpublished`,
`partial_failed`, `failed`다. 실패와 성공이 섞이면 `partial_failed`, 성공과 미게시가
섞이면 `partial_unpublished`다. 실행 중단 복구 시 최상위는 `failed`이며 이미 커밋된
학기는 `completed`와 실제 건수를 유지한다. 시각은 UTC ISO 8601(`+00:00`)이다.

**202는 접수 확인이다.** 완료·성공·미게시 판단은 `results`를 확인해야 한다.
실패한 학기의 저장 건수는 0이고, 다른 학기의 성공 건수를 합쳐 실패를 숨기지 않는다.

## 실행과 DB 준비

현재 저장소는 `crawler/config.py`의 `DB_CONFIG`를 공유한다. 환경 변수로 자동 변경되는
설정이라고 가정하지 말고 배포 대상의 실제 설정을 확인한다. Spring과 같은 환경의
MySQL/schema를 사용해야 한다. 학사일정도 기존 SQLAlchemy 세션 방식을 사용하되
접수 지연을 제한하는 별도 풀을 둔다(연결 3초, 읽기/쓰기 5초, 풀 대기 2초).
기존 학식·공지 풀 설정은 유지한다. 학사일정 세션에는 기존 SQL mode를 유지하면서
`STRICT_ALL_TABLES`를 추가해 DB가 값을 조용히 잘라 저장하는 동작도 거절한다.
서버 전역 설정이나 테이블을 변경하는 설정은 아니다.

적용 순서:

1. 대상 DB에서 **Spring의 `docs/0005_academic_calendar.sql`**을 적용한다.
2. 같은 DB 설정을 사용하는 Python 코드에서 이력 전용 마이그레이션을 실행한다.
   `python -m crawler.academic_calendar.migrate`
3. API 프로세스를 새 코드로 재시작한다. 기존 진입점은
   `python -m uvicorn api_main:app --host 127.0.0.1 --port 8000`이다.
4. Spring `ACADEMIC_CALENDAR_CRAWLER_BASE_URL`을 해당 API 주소로 설정한다.
   `ACADEMIC_CALENDAR_SCHEDULER_ENABLED=true`는 Spring에서 관리한다.
5. 격리된 연동 환경에서 POST 후 run_id로 결과와 공개 캘린더를 확인한다.

Python 이력 마이그레이션은 다음 두 InnoDB 테이블만 없을 때 생성한다.

- `academic_calendar_crawl_runs`: 실행별 요청, 학기별 결과 JSON, UTC DATETIME(6) 시각과 오류.
- `academic_calendar_crawl_lock`: id=1인 전역 학사일정 잠금 행, 소유 run_id, UTC 만료 시각.

기존 이력을 지우지 않으며 반복 실행할 수 있다. 마이그레이션 계정은 CREATE 권한,
실행 계정은 두 이력 테이블의 SELECT/INSERT/UPDATE 및 학교 일정 테이블의
SELECT/INSERT/UPDATE 권한이 필요하다. 실행 계정에 DELETE/ALTER 권한은 필요하지 않다.
기존 `scripts/deploy.sh`는 학식 이력만 준비하므로 학사일정 마이그레이션은 **별도로** 실행한다.
API 시작/요청 시 테이블 생성이나 자동 스키마 변경을 수행하지 않는다.

`school_calendar_events`의 필수 컬럼, 문자열 길이, 날짜/시각 타입, 원본 유일 키,
InnoDB 여부를 반영 전에 검사한다. 누락/불일치는 학기 실패 이력에 남긴다.
실행 접수·상태 조회 전에는 이력 테이블의 컬럼 타입, 기본 키, InnoDB 여부도 검사한다.
트랜잭션을 보장하지 않는 이력 테이블이나 잠금 행 누락을 정상 상태로 취급하지 않는다.
이력 스키마 자체가 없거나 DB가 연결되지 않아 이력을 저장할 수 없는 경우에는 API 503과
프로세스 로그로 확인한다.

## 저장과 보존 규칙

- 키: `(source='HANSEO', academic_year, semester, source_event_id)`.
- 모든 행사 항목을 저장한다. 원본 `date...` ID 문자열 전체를 보존한다.
- 기존 행의 id와 created_at을 유지한다. **created_at은 학교 최초 게시 시각이 아니라
  우리 DB 최초 저장 시각**이다.
- 날짜·제목·유형·source_url 변경 또는 비활성 행 재등장은 `updated`로 집계한다.
  동일하면 `unchanged`다. 정상 확인된 행의 last_synced_at은 항상 갱신하며,
  updated_at은 실제 원본 정보나 active 값이 바뀔 때 갱신한다.
- created_at/updated_at/last_synced_at은 UTC DATETIME(6), start_date/end_date는 학교
  현지 날짜 자체다. 제목 500자·ID 100자·URL 1000자 초과를 자르지 않고 실패 처리한다.
- 학기 전체 파싱·검증 후 일정 변경과 해당 학기 성공 이력을 한 트랜잭션으로 커밋한다.
  이력 기록을 포함해 저장 도중 실패하면 그 학기 변경을 모두 롤백한다.
  DB 커밋은 성공했지만 완료 응답이 끊긴 경우에는 DB의 원자적으로 저장된 학기 결과를
  다시 확인한다. 이미 `completed`/`unpublished`인 결과를 `failed`로 덮지 않는다.
- HTTP/타임아웃/파싱/선택 연도·학기 불일치/원본 충돌/월 누락 시 기존 데이터를 보존한다.
- 두 뷰 모두 6개 월 영역이 있고 모든 일정이 0건이면 `unpublished`다. HTML 자체가
  비었거나 표/월이 없으면 `failed`다. 둘 다 기존 일정을 비활성화하지 않는다.
- 완전하고 비어 있지 않은 스냅샷에서만 사라진 HANSEO 해당 학년도·학기 행을 active=0으로
  바꾼다. 기존에 있던 핵심 일정 유형이 통째로 사라졌으면 **해당 학기 반영 전체를 보류**하고
  `failed`와 누락 유형을 기록한다. 같은 ID의 제목이 달라져 핵심 일정이 OTHER로 바뀐 경우도
  포함한다. 이전 시험을 보존하면서 새 ID의 시험을 추가해 활성 시험이 중복되는 일을 막는다.
  정상적인 완전 스냅샷에서 핵심 유형을 유지한 채 ID만 교체되면 이전 ID는 비활성화하고
  새 ID를 저장한다. 한 학기의 검증 실패가 다른 학기 수집을 막지 않는다.
- MANUAL, 다른 학년도·학기와 학생회/개인 일정은 변경하지 않는다. 전체 삭제·재등록은 없다.

## HTML 검증과 분류

원본: <https://www.hanseo.ac.kr/schdList.do?m=0405&s=hs>

`form[name=calForm]`의 `schdYear option[selected]`와 hidden `section` 값을 요청과
비교한다. 연도 옵션이 있다는 이유로 게시되었다고 판단하지 않는다.
`table_style02`(PC), `table_style03`(모바일)의 월별 **행사내용** 열만 파싱한다.
달력 숫자는 날짜 후보로 읽지 않는다. 각 뷰에 1학기 3~8월 또는 2학기 9~2월의
6개 영역이 순서대로 있어야 한다. 빈 월은 허용하되 영역 누락·중복은 실패다.
두 뷰의 ID 집합과 내용이 일치해야 하며, 한쪽 항목 누락도 불완전 응답으로 처리한다.
학교가 표 구조를 변경하거나 한 뷰를 제거하면 추측해서 반영하지 않고 실패한다.

단일 날짜, 같은 달 범위, 명시된 월 경계와 12월→1월 범위를 처리한다.
2학기 1·2월은 요청 학년도+1이다. 해석 불가능하거나 역전된 날짜는 실패다.
일반 행사·수강신청·등록 등은 8월→9월, 2월→3월까지 이어져도 원본 기간을 그대로 저장한다.
학년도·학기 메타데이터는 종료 날짜가 아닌 조회 페이지 기준이다.
HTML 엔티티·줄바꿈·연속 공백을 정리하고 제목 의미를 보존한다.
제목에 다음 학기 번호가 있어도 페이지의 학년도·학기를 바꾸지 않는다.

정규 학기개시일, 중간고사, 기말고사, 하계/동계방학 시작만 각각
`SEMESTER_START`, `MIDTERM`, `FINAL`, `VACATION_START`로 분류한다.
나머지는 `OTHER`로 저장한다. 학기개시일 30/60/90일, 중간 강의평가, 계절학기,
계절학기 수강신청은 핵심 일정이 아니다. 핵심 유형이 같은 학기에 2건 이상 있거나
시작 일정이 날짜 범위로 표현되면 해당 학기를 반영하지 않는다.
중간고사/기말고사 순서 역전·겹침, 시험과 방학의 겹침, 공식 학기개시일 이전의 시험,
다음 학기로 넘어가는 핵심 일정, 핵심 일정 제목에 명시된 학년도·학기의 불일치도 실패다.
이는 원본의 상충 검증이며, 개강 날짜를 계산하거나 공식 날짜를 바꾸는 처리는 없다.

## 중복 실행과 재시작

확인한 `hanseo-crawler.service`의 ExecStart에는 `--workers`가 없으며 단일 Uvicorn
프로세스 구성이다. 실제 운영 서버의 systemd override·worker·인스턴스 수는 확인하지 못했다.

학사일정은 메모리 잠금에 의존하지 않는다. 동일 MySQL/schema를 공유하는 요청들이
하나의 잠금 행에 조건부 UPDATE로 소유권을 획득한다. 모든 저장/이력 트랜잭션은
소유 run_id와 만료 여부를 검사하며 잠금 행을 커밋까지 잡는다. 만료된 작업이 늦게
돌아와도 새 실행의 데이터나 이력을 덮어쓸 수 없다. 동시 실행은 학년도·학기에 관계없이
학사일정 전체에서 하나다. 기존 학식/공지의 메모리 잠금 방식을 확장한 것은 아니다.

lease는 DB의 UTC 시계 기준 **5분**이며 학기 시작과 반영/이력 저장 때 갱신한다.
프로세스 종료 직후에는 남은 lease 동안 409일 수 있다. 만료 후 다음 POST 또는 GET
상태 조회가 중단 이력을 failed로 복구한다. 자동 재실행·내부 예약·heartbeat 스케줄은 없다.
DB 장애로 종료 이력을 쓸 수 없어도 같은 복구 규칙을 따른다. 이미 커밋된 학기 결과는 보존한다.
5분 넘게 응답을 흘려보내는 비정상 HTTP 등으로 lease가 끝나면 뒤늦은 저장은 거절된다.

이 설계의 MySQL 동시 실행 검증은 별도 opt-in 테스트로 제공한다. 아래 로컬 결과를
실제 운영 다중 인스턴스 검증으로 해석하지 않는다.

## 로컬 검증

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

기본 테스트는 실제 학교 HTML fixture, 모의 HTTP와 테스트별 임시 SQLite 파일만 사용한다.
운영 DB 설정으로 연결하지 않는다. 기본 테스트 실행 중 학교 사이트를 요청하지 않는다.
요청 검증은 Pydantic 2를 사용하므로 `requirements.txt`에 `pydantic>=2,<3`을 명시했다.

최종 검토 후 실행 결과: **62개 중 59개 통과, 실패/오류 0개, MySQL 전용 3개 건너뜀**.
학사일정 기본 테스트 43개와 기존 학식 테스트 16개가 통과했다. Windows 샌드박스 안에서는
TestClient의 asyncio 로컬 소켓 생성이 멈춰 샌드박스 밖에서 같은 격리 테스트를 실행했다.

추가 회귀 검증은 핵심 일정 누락과 ID 교체의 조합, 핵심 일정의 OTHER 재분류,
커밋 성공 후 응답 유실(성공/미게시), 일반 일정의 학기 경계 통과, 시험·방학 상충,
잠금 행 누락과 잘못된 MySQL 스키마를 포함한다. 응답 유실은 격리 SQLite 커밋 직후
예외를 주입했고, MySQL 스키마 판별은 SQLAlchemy dialect 타입과 모의 inspector로 검사했다.
실제 MySQL 통신 단절·InnoDB 동시성 검증을 수행한 것은 아니다.

2026-11-03 실제 학교 조회 결과:

| 요청 | 응답 선택값 | 중복 제거 후 | 중간고사 | 기말고사 | 방학 시작 |
|---|---|---:|---|---|---|
| 2026 / 1학기 | 2026 / 1 | 34건 | 4/20~4/24 | 6/15~6/19 | 6/22 |
| 2026 / 2학기 | 2026 / 2 | 33건 | 10/20~10/26 | 12/15~12/21 | 12/22 |

2학기 12/22~01/13은 2026-12-22~2027-01-13, 1월 04~06일은
2027-01-04~2027-01-06으로 확인했다. HTTP 읽기와 파싱만 했으며 실제 DB 저장은 하지 않았다.
원본 응답은 `tests/fixtures/academic_calendar/2026_1.html`, `2026_2.html`에 보관했다.
미게시·손상·충돌 응답은 원본 구조를 변형한 모의 사례이며, 학교가 실제 그런 응답을
보냈다고 주장하지 않는다. 2027 동작도 fixture 기반 검증이며 다음 학년도 사이트는 조회하지 않았다.

MySQL 8 검증은 **빈 로컬 테스트 DB**를 별도로 준비한 뒤 실행한다. DB명은
`hanseo_calendar_test_<소문자/숫자/밑줄 suffix>`이어야 한다.

```bash
export ACADEMIC_CALENDAR_TEST_MYSQL_URL='mysql+pymysql://<test-user>:<test-password>@127.0.0.1:3306/hanseo_calendar_test_local'
python -m unittest discover -s tests -p 'test_academic_calendar_mysql.py' -v
```

PowerShell에서는 같은 값을 `$env:ACADEMIC_CALENDAR_TEST_MYSQL_URL`에 설정한다.
URL이 없으면 3개 테스트를 skip한다. localhost 이외의 호스트, 지정 접두사와 다른
DB명, 이미 테이블이 있는 DB는 테스트가 거절한다. 테스트는 Spring 변경 전 표를
격리 DB에 만들고 **Spring 원본 SQL 사본**을 적용한 뒤 BIT(1)/DATE/DATETIME(6),
재수집, 별도 연결의 동시 claim, 이력 실패 시 롤백을 검사한다. 종료 시 테스트 표를 제거한다.
사본은 테스트 자료이며 운영 마이그레이션 진입점은 Spring 원본이다.

이번 환경에서는 Docker 엔진에 연결할 수 없어 실제 MySQL 8 테스트는 미실행이다.
실제 Spring→Python HTTP 호출, 운영 DB SQL 적용·저장, 서버 재시작/배포는 검증하지 않았다.

## Spring 담당자 전달 사항

- 기존 POST 요청/202 응답 계약 변경은 없다. 상태 조회 API와 이력 스키마만 추가했다.
- Python 이력 migration을 Spring SQL 다음, 첫 API 호출 전에 별도로 실행해야 한다.
- 매일 04시 Asia/Seoul의 현재 달력연도 선택과 재시도 시점은 Spring 책임이다.
- 409는 진행 중이며 실패한 신규 수집이 아니다. 실제 완료 여부는 run_id로 조회한다.
- 원본 공식 학기개시일은 그대로 저장한다. 개강일 계산, 공휴일 동기화, D-day, 푸시,
  공개 캘린더와 수업/셔틀 알림 제어는 Python에 추가하지 않았다.
- 커밋·푸시·배포·운영 DB 변경은 이번 작업에서 수행하지 않았다.
