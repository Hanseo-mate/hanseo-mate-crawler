# 스프링 채팅에 전달할 프롬프트

한서메이트 학식 크롤링 스케줄을 아래 계약에 맞게 수정해줘. 이번 작업은 Spring 저장소만 수정하고, Python 크롤러는 이미 아래 동작으로 수정했어.

먼저 현재 체크아웃 브랜치를 확인해줘. 로컬 `feature/campusmap`에는 오래된 학식 코드가 있었고, 비교한 `origin/main` 스냅샷은 `6e75a08`이었어. 현재 원격 main을 확인하고, 기존 로컬 변경은 보존한 채 적절한 작업 브랜치/워크트리에서 진행해줘.

## 원하는 동작

- 한국 시간 `Asia/Seoul`, 월~금 `01, 03, 05, 07, 09, 11, 13, 15, 17시`에 호출해줘. cron은 `0 0 1-17/2 * * MON-FRI`로 한 곳에서 관리할 수 있어.
- 매일 01시에 각 식당의 식단을 새로 확인하고, 그날 미완료 식당만 17시까지 2시간 간격으로 다시 확인해줘. 주말/17시 이후에는 자동 호출하지 않아. 17시에 시작한 수집은 완료될 때까지 진행해.
- 하루 최대 9번 호출 시점이며, 별도 최대 5회 제한은 사용하지 않아.
- 배포 직후에도 해당 날짜의 다음 예정 시각에 실행돼야 해. 수집이 완료되지 않았다면 다음 평일에 다시 시도해.

## 수정한 크롤러 계약

정기 실행 시 모든 시각에 동일하게 아래 요청 한 번만 보내면 돼. 식당별로 네 번 호출하지 않아.

```http
POST /cafeteria-crawl/run
Content-Type: application/json

{"mode":"background","only_pending":true}
```

- `restaurant_types`를 생략하면 전체 식당이 대상이야. 특정 식당을 요청할 때만 문자열 배열을 사용해.
- `only_pending=true`이면 크롤러가 DB의 식당별 당일 성공 기록을 확인해 완료한 식당은 `skipped` 처리해. 그래서 스프링에서 DB에 이번 주 메뉴가 있다는 이유로 전체 요청을 생략하면 안 돼.
- 당일 성공은 이번 주의 유효한 식단을 학교에서 읽고 DB 저장 또는 기존 내용과 동일함을 확인한 경우야. `completed`와 `unchanged` 모두 성공이고, 어제 성공한 기록은 오늘을 건너뛰는 근거가 아니야.
- 지난주/다음 주 식단, 빈 결과, HTTP/파싱/DB 실패는 성공으로 기록되지 않아. 한 식당 실패가 다른 식당의 성공을 취소하거나, 다른 식당 성공이 실패한 식당의 후속 시도를 막지 않아.
- 크롤러 내부 timer는 제거했어. 스프링이 시간을 관리하며 크롤러는 요청 한 번당 한 번만 수집해.
- 기존 `url`, `restaurant_type` 단수 필드 대신 위 계약을 사용해.
- 수동 강제 수집은 `only_pending=false`(기본값)로 할 수 있어. 이 경우 그날 성공한 식당도 다시 조회해.

## 상태와 중복 처리

- 백그라운드 응답의 `status=starting`, `run_id`는 접수 확인이야. DB 저장 완료로 기록하면 안 돼.
- 진행 중인 작업에 중복 요청하면 `409`야. 이미 진행 중으로 로그를 남기고 이번 시각은 종료해. 다른 실패도 로그를 남기고 다음 정기 시각에 재요청해. 즉시 재시도 루프를 추가하지 마.
- `GET /cafeteria-crawl/status`는 현재 프로세스의 최근 실행 상태야. `results` 아래에 식당별 결과가 있어. top-level `menus`로 받지 마.
- `GET /cafeteria-crawl/daily-status`는 DB에서 읽는 영구 상태야. 최상위 `business_date`(한국 날짜), `pending_restaurant_types`, `results`가 있어. 식당별 `completed_today`, `status`, `business_date`, `run_id`, `last_attempt_at`, `last_success_date`, `last_success_at`, `error`를 제공해. 시각은 UTC ISO 8601, 날짜는 KST야.
- `/status`의 `retry_count=0`, `max_retry_count=0`, `next_retry_at=null`은 내부 타이머 제거 후 호환용 값이야. 수집 완료 판정에 사용하지 마.
- 재시작으로 `running` 상태가 남아도 크롤러 프로세스에 실제 실행 중인 작업이 없으면 다음 호출에서 재시도해.
- 단일 크롤러 프로세스가 중복 실행을 방지하는 현재 systemd 구성을 유지해. 다중 worker/인스턴스 운영이 필요하면 분산 실행 잠금을 별도로 설계해야 해.

## 코드와 배포

- `CafeteriaScheduler`, 요청 DTO, `CafeteriaCrawlerClient`와 관련 테스트/문서를 수정해줘.
- 기존 `CafeteriaSyncOrchestrator`/`CafeteriaRetryStateService`의 사용 여부를 확인하고 중복 예약 경로를 활성화하지 마. 현재 스케줄 호출은 Python이 DB에 직접 저장하는 방식이야.
- Python 배포 스크립트가 `cafeteria_crawl_progress` 테이블을 준비해. Spring에서 같은 테이블을 별도 마이그레이션으로 생성하거나 데이터를 초기화할 필요는 없어.
- 월~금 01~17시 포함, 19시·주말 제외, 매일 동일 요청, `only_pending=true` 전송, 409 및 HTTP 실패 이후 다음 일정 유지가 테스트로 검증돼야 해.
- 운영 DB/서버에 접속할 수 없는 상태야. 코드·로컬 테스트 결과와 실제 배포/실행 확인을 구분해서 알려줘.
- 배포 순서는 크롤러 새 코드 배포 후 스프링 새 코드 배포야. 두 코드가 모두 적용돼야 최종 일정이 동작해.
