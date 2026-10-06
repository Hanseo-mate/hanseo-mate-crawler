# Hanseo Mate Crawler Integration Guide

## Overview

이 문서는 IntelliJ 기반 Spring Boot 프로젝트에서 한서메이트 Python 크롤러를 연동할 때 필요한 기준 정보를 정리한 문서입니다.

현재 역할 분리는 아래와 같습니다.

- Python: 크롤링 실행, HTML 정제, Base64 이미지 파일 저장, DB 적재
- Spring Boot: DB 조회, 비즈니스 로직, 프론트엔드/API 제공
- FastAPI: Python 크롤러 운영 제어용 API만 제공

조회 API는 Python 쪽에 만들지 않았습니다. Spring Boot가 MySQL을 직접 조회하는 구조를 기준으로 정리합니다.

## Runtime Summary

- Base site: `https://www.hanseo.ac.kr`
- Image save directory: `/home/hanseo-mate/images`
- Public image base URL: `http://34.64.250.12/images/`
- Database name: `hanseo_mate`
- Python API entrypoint: `api_main.py`
- Python crawler CLI entrypoint: `main.py`

## Notice Types

`notices.notice_type` 컬럼에는 아래 값이 들어갑니다.

| notice_type | 의미 | board_id | menu_code |
| --- | --- | --- | --- |
| `academic` | 학사공지 | `298` | `040101` |
| `general` | 일반공지 | `299` | `040102` |
| `scholarship` | 장학공지 | `301` | `040104` |
| `graduate` | 대학원공지 | `302` | `040105` |

## Restaurant Types

식단 크롤러 요청의 `restaurant_types` 배열 원소는 아래 enum 중 하나입니다.

| restaurant_type | 의미 |
| --- | --- |
| `MAIN_STUDENT` | 본교 학생식당 |
| `MAIN_STAFF` | 본교 교직원식당 |
| `TAEAN_STUDENT` | 태안 학생식당 |
| `TAEAN_STAFF` | 태안 교직원식당 |

## Database Schema

### 1. `notices`

공지 본문과 메타데이터를 저장하는 메인 테이블입니다.

```sql
CREATE TABLE IF NOT EXISTS notices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    notice_type VARCHAR(50) NOT NULL,
    origin_notice_id VARCHAR(32) NOT NULL,
    title VARCHAR(500) NOT NULL,
    source_url VARCHAR(1024) NOT NULL,
    content_html LONGTEXT NOT NULL,
    change_summary TEXT NULL,
    author VARCHAR(100) NOT NULL,
    post_date DATE NOT NULL,
    is_hot BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uk_notices_notice_type_origin_notice_id (notice_type, origin_notice_id),
    KEY idx_notices_notice_type_post_date (notice_type, post_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

컬럼 설명:

| 컬럼명 | 타입 | 설명 |
| --- | --- | --- |
| `id` | `BIGINT UNSIGNED` | 내부 PK |
| `notice_type` | `VARCHAR(50)` | 공지 종류. `academic`, `general`, `scholarship`, `graduate` 중 하나 |
| `origin_notice_id` | `VARCHAR(32)` | 한서대학교 원본 공지 ID |
| `title` | `VARCHAR(500)` | 공지 제목 |
| `source_url` | `VARCHAR(1024)` | 원본 공지 상세 URL |
| `content_html` | `LONGTEXT` | 공지 본문 HTML |
| `change_summary` | `TEXT` | 기존 공지가 수정된 경우 변경 요약. 신규 공지는 `NULL` 가능 |
| `author` | `VARCHAR(100)` | 작성자 |
| `post_date` | `DATE` | 게시일 |
| `is_hot` | `BOOLEAN` | HOT 공지 여부 |

주의 사항:

- 유니크 기준은 `origin_notice_id` 단독이 아니라 `(notice_type, origin_notice_id)` 입니다.
- 같은 `origin_notice_id`가 다른 게시판에 존재해도 충돌하지 않도록 설계했습니다.
- `content_html`은 `LONGTEXT`를 유지합니다.
- 본문 내 Base64 임베디드 이미지는 파일로 추출한 뒤 짧은 URL로 치환되므로 `Data too long` 가능성을 낮췄습니다.

### 2. `notice_files`

공지 첨부파일 목록을 저장하는 테이블입니다.

```sql
CREATE TABLE IF NOT EXISTS notice_files (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    notice_id BIGINT UNSIGNED NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_url VARCHAR(512) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_notice_files_notice_id (notice_id),
    CONSTRAINT fk_notice_files_notice_id
        FOREIGN KEY (notice_id) REFERENCES notices (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

컬럼 설명:

| 컬럼명 | 타입 | 설명 |
| --- | --- | --- |
| `id` | `BIGINT UNSIGNED` | 내부 PK |
| `notice_id` | `BIGINT UNSIGNED` | `notices.id` FK |
| `file_name` | `VARCHAR(255)` | 첨부파일 이름 |
| `file_url` | `VARCHAR(512)` | 한서대학교 원본 첨부파일 URL |

### 3. `daily_menus`

식당/날짜 단위 식단 헤더를 저장하는 테이블입니다.

```sql
CREATE TABLE IF NOT EXISTS daily_menus (
  id INTEGER NOT NULL AUTO_INCREMENT,
  restaurant_type ENUM('MAIN_STUDENT', 'MAIN_STAFF', 'TAEAN_STUDENT', 'TAEAN_STAFF') NOT NULL,
  menu_date DATE NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_daily_menu_restaurant_date (restaurant_type, menu_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

컬럼 설명:

| 컬럼명 | 타입 | 설명 |
| --- | --- | --- |
| `id` | `INT` | 내부 PK |
| `restaurant_type` | `ENUM` | 식당 구분 |
| `menu_date` | `DATE` | 식단 기준 날짜 |

### 4. `meal_sections`

한 날짜의 점심/저녁 및 코너 구분을 저장하는 테이블입니다.

```sql
CREATE TABLE IF NOT EXISTS meal_sections (
  id INTEGER NOT NULL AUTO_INCREMENT,
  daily_menu_id INTEGER NOT NULL,
  meal_time ENUM('LUNCH', 'DINNER') NOT NULL,
  corner_name VARCHAR(100) NOT NULL,
  price INTEGER NULL,
  dishes JSON NOT NULL,
  raw_text TEXT NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT fk_meal_sections_daily_menu_id
    FOREIGN KEY (daily_menu_id) REFERENCES daily_menus (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

컬럼 설명:

| 컬럼명 | 타입 | 설명 |
| --- | --- | --- |
| `id` | `INT` | 내부 PK |
| `daily_menu_id` | `INT` | `daily_menus.id` FK |
| `meal_time` | `ENUM` | 점심 또는 저녁 |
| `corner_name` | `VARCHAR(100)` | `1코너`, `A코너`, `특식` 등 코너명 |
| `price` | `INT NULL` | 원 단위 가격. 파싱 실패 시 `NULL` |
| `dishes` | `JSON` | 반찬명 문자열 배열 |
| `raw_text` | `TEXT` | 파싱 전 코너 텍스트. 앱 fallback 용도 |

기존 `dishes` 테이블은 사용하지 않습니다. Python 시작 시 레거시 테이블을 삭제하고 구형
`meal_sections`를 새 구조로 한 번 재생성합니다.

## Data Behavior

### Sync Rules

Python 크롤러는 `notices`에 대해 신규 공지는 `INSERT`, 기존 공지는 변경이 있을 때만 `UPDATE` 합니다.

기준 키:

- `(notice_type, origin_notice_id)`

동작 규칙:

- 목록 페이지에서 `(notice_type, origin_notice_id)` 기준으로 기존 공지를 먼저 조회합니다.
- 목록 페이지에서 먼저 날짜 컷오프를 적용합니다. 예를 들어 실행일이 `2026-07-30`이면 `2025-07-30` 이전 공지는 수집 대상에서 제외합니다.
- 단, `is_hot = 1` 인 공지는 날짜가 1년을 넘어도 예외적으로 계속 수집 대상에 포함합니다.
- 기존 공지는 목록 메타데이터(`title`, `author`, `post_date`, `is_hot`)를 먼저 비교합니다.
- 목록 메타데이터가 동일하면 상세 크롤링과 DB 쓰기를 모두 건너뜁니다.
- 목록 메타데이터가 달라진 기존 공지만 상세 페이지를 다시 조회하고, 실제 본문/첨부 포함 변경분이 있을 때만 `UPDATE` 합니다.
- 기존 공지 update가 발생하면 `change_summary` 필드에 변경 이유를 별도로 기록할 수 있습니다. 예: `HOT 해제`, `제목 변경`, `본문/첨부 변경`
- 신규 공지만 상세 페이지를 조회한 뒤 `INSERT` 합니다.
- 동시 실행 등으로 선조회 이후 중복이 생겨도 DB 유니크 키로 한 번 더 막고, 중복 충돌 시 추가 쓰기 없이 종료합니다.

첨부파일은 신규 공지 insert 시 저장하고, 기존 공지 update가 실제로 필요할 때만 교체합니다.

`change_summary` 예시:

- `HOT 해제`
- `제목 변경`
- `작성일 변경`
- `본문 변경`
- `첨부파일 변경`
- `제목 변경, 본문 변경`

### Expired Data Deletion

크롤링 실행 마지막 단계에서 작성일(`post_date`) 기준 1년이 지난 비HOT 데이터만 삭제됩니다.

예를 들어 실행일이 `2026-07-30`이면 `2025-07-30` 이전이면서 `is_hot = 0` 인 공지가 삭제 대상입니다.

외래키 제약 오류를 피하기 위해 `notice_files`를 먼저 삭제하고, 이후 `notices`를 삭제합니다. 두 쿼리는 하나의 트랜잭션으로 묶어 실행합니다.

```sql
DELETE notice_files
FROM notice_files
INNER JOIN notices ON notices.id = notice_files.notice_id
WHERE notices.notice_type = ?
  AND notices.is_hot = FALSE
  AND notices.post_date < DATE_SUB(CURDATE(), INTERVAL 1 YEAR);
```

```sql
DELETE FROM notices
WHERE notice_type = ?
  AND is_hot = FALSE
  AND post_date < DATE_SUB(CURDATE(), INTERVAL 1 YEAR);
```

### Cafeteria Sync Rules

식단 크롤러는 새 주차 내용이 기존 내용과 다를 때만 요청받은 `restaurant_type` 데이터를 교체합니다.

동작 규칙:

- Python API는 전달받은 `url`에서 HTML을 가져옵니다.
- `div.fd_info p.txt`에서 기준 날짜를 파싱한 뒤 해당 주의 월요일을 계산합니다.
- `div.fd_table table tbody tr`를 월요일부터 금요일까지 순회하며 `menu_date`를 계산합니다.
- 현재 주차의 날짜, 식당, 식사 시간, 코너명, 가격, 반찬 배열, 원문을 정규화해 완전 비교합니다.
- 한국 시간 기준 이번 주 식단인지 먼저 확인합니다. 지난주/다음 주 식단이나 빈 결과는 실패로 처리하고 기존 식단을 유지합니다.
- 내용이 같으면 식단을 다시 저장하지 않고 상태를 `unchanged`로 반환합니다. 당일 수집 성공 기록은 갱신합니다.
- 내용이 다르면 같은 `restaurant_type`의 과거 `daily_menus`를 모두 hard delete한 뒤 새 데이터를 저장합니다.
- 상위 `daily_menus` 삭제 시 하위 `meal_sections`는 DB cascade로 함께 삭제됩니다.
- Python 내부 재시도 timer는 없습니다. 호출 한 번에 선택한 식당을 한 번씩 수집합니다.
- Spring이 월~금 한국 시간 01, 03, 05, 07, 09, 11, 13, 15, 17시에 `only_pending=true`로 호출합니다. 성공한 식당은 당일 후속 호출에서 건너뛰며 다음 한국 날짜에는 다시 수집합니다.
- 식당별 성공 날짜와 마지막 시도는 `cafeteria_crawl_progress`에 영구 저장됩니다. `/cafeteria-crawl/daily-status`로 확인할 수 있습니다.
- 식단 변경과 성공 기록은 같은 DB 트랜잭션에서 확정합니다. 재시작으로 중단된 `running` 기록은 다음 호출에서 다시 시도합니다.
- 식단 조회 API는 Python에 두지 않고 Spring Boot가 MySQL을 직접 조회하는 구조를 유지합니다.

파싱 규칙:

- `td.get_text(separator='\n', strip=True)`로 `<br>` 줄바꿈을 보존합니다.
- 줄 전체가 하이픈 3개 이상인 구분선을 기준으로 점심/저녁 코너를 각각 분리합니다.
- `1코너`, `A 코너`, `특식` 등 코너명을 감지하며 없으면 순서 기반 코너명을 사용합니다.
- `5.5`, `5,500`, `5500` 가격은 모두 정수 `5500`으로 저장합니다.
- `(한식)`, `(일품)`, `(금요일 한식만 운영)` 같은 괄호 안내 문구는 저장하지 않습니다.
- 메뉴명 끝의 `*`, `**`는 제거하고 반찬명 문자열만 JSON 배열에 저장합니다.
- 각 코너의 정제 전 텍스트는 `raw_text`에 함께 저장합니다.

동기 실행의 상태 응답은 `results[식당타입].menus`에 아래 camelCase 구조를 포함합니다.

```json
{
  "menuDate": "2026-08-31",
  "restaurantType": "MAIN_STUDENT",
  "mealSections": [
    {
      "mealTime": "LUNCH",
      "cornerName": "1코너",
      "price": 5500,
      "dishes": ["쌀밥", "불고기"],
      "rawText": "1코너 (5.5)\n쌀밥\n불고기"
    }
  ]
}
```

## Image Processing Rules

공지 본문 HTML 내부 `img src`가 아래 형식이면:

```text
data:image/png;base64,iVBORw0KG...
```

Python 크롤러는 다음 순서로 처리합니다.

1. Base64 이미지를 디코딩합니다.
2. `/home/hanseo-mate/images/{uuid}.{ext}` 에 파일로 저장합니다.
3. 본문 HTML의 `img src`를 `http://34.64.250.12/images/{uuid}.{ext}` 로 치환합니다.
4. 최종 치환된 HTML을 `notices.content_html` 에 저장합니다.

즉 Spring Boot에서 DB 조회 시 `content_html` 안의 이미지는 로컬 경로가 아니라 HTTP URL 형태입니다.

예시:

```html
<img src="http://34.64.250.12/images/a1b2c3d4e5f6.png">
```

## Recommended Query Patterns For Spring Boot

### 1. 공지 목록 조회

```sql
SELECT
    id,
    notice_type,
    origin_notice_id,
    title,
    source_url,
  change_summary,
    author,
    post_date,
    is_hot
FROM notices
WHERE notice_type = ?
ORDER BY post_date DESC, id DESC
LIMIT ? OFFSET ?;
```

### 2. 공지 상세 조회

```sql
SELECT
    id,
    notice_type,
    origin_notice_id,
    title,
    source_url,
    content_html,
  change_summary,
    author,
    post_date,
    is_hot
FROM notices
WHERE id = ?;
```

### 3. 첨부파일 조회

```sql
SELECT
    id,
    notice_id,
    file_name,
    file_url
FROM notice_files
WHERE notice_id = ?
ORDER BY id ASC;
```

### 4. 공지 타입별 최신 공지 조회

```sql
SELECT
    id,
    notice_type,
    title,
    post_date,
    source_url
FROM notices
WHERE notice_type IN ('academic', 'general', 'scholarship', 'graduate')
ORDER BY post_date DESC, id DESC;
```

## FastAPI Operational API

Python API는 운영 제어용만 제공합니다.

Base URL 예시:

```text
http://34.64.250.12:8000
```

### 1. Health Check

- Method: `GET`
- Path: `/health`

응답 예시:

```json
{
  "status": "ok",
  "available_notice_types": [
    "academic",
    "general",
    "scholarship",
    "graduate"
  ],
  "available_restaurant_types": [
    "MAIN_STUDENT",
    "MAIN_STAFF",
    "TAEAN_STUDENT",
    "TAEAN_STAFF"
  ]
}
```

용도:

- Python API 프로세스 생존 확인
- 현재 지원하는 공지 타입 확인

### 2. Crawl Status

- Method: `GET`
- Path: `/crawl/status`

응답 예시:

```json
{
  "run_id": "4f44e6d0c8d24abeb4ab1a9ea2f9b2f3",
  "status": "running",
  "requested_notice_types": ["academic", "general"],
  "started_at": "2026-07-22T12:34:56.000000+00:00",
  "finished_at": null,
  "current_notice_type": "academic",
  "results": {},
  "error": null
}
```

`status` 값 의미:

| 값 | 의미 |
| --- | --- |
| `idle` | 아직 실행 이력 없음 또는 대기 상태 |
| `running` | 현재 크롤링 실행 중 |
| `completed` | 마지막 실행 성공 완료 |
| `failed` | 마지막 실행 실패 |

`results` 예시:

```json
{
  "academic": {
    "pages_processed": 50,
    "notices_inserted": 734,
    "notices_updated": 12,
    "notices_failed": 3,
    "notices_deleted": 18
  },
  "general": {
    "pages_processed": 50,
    "notices_inserted": 680,
    "notices_updated": 4,
    "notices_failed": 0,
    "notices_deleted": 18
  }
}
```

### 3. Trigger Crawl

- Method: `POST`
- Path: `/crawl/run`

요청 바디:

```json
{
  "notice_types": ["academic", "general"],
  "mode": "background"
}
```

필드 설명:

| 필드 | 타입 | 필수 여부 | 설명 |
| --- | --- | --- | --- |
| `notice_types` | `string[] \| null` | 선택 | 실행할 공지 타입 목록. `null`이면 전체 실행 |
| `mode` | `background \| sync` | 선택 | `background`는 즉시 반환, `sync`는 크롤링 완료 후 반환 |

`mode=background` 응답 예시:

```json
{
  "run_id": "4f44e6d0c8d24abeb4ab1a9ea2f9b2f3",
  "status": "running",
  "requested_notice_types": ["academic", "general"],
  "started_at": "2026-07-22T12:34:56.000000+00:00",
  "finished_at": null,
  "current_notice_type": null,
  "results": {},
  "error": null
}
```

`mode=sync` 응답 예시:

```json
{
  "run_id": "4f44e6d0c8d24abeb4ab1a9ea2f9b2f3",
  "status": "completed",
  "requested_notice_types": ["academic"],
  "started_at": "2026-07-22T12:34:56.000000+00:00",
  "finished_at": "2026-07-22T12:39:22.000000+00:00",
  "current_notice_type": null,
  "results": {
    "academic": {
      "pages_processed": 50,
      "notices_inserted": 734,
      "notices_updated": 12,
      "notices_failed": 3,
      "notices_deleted": 18
    }
  },
  "error": null
}
```

오류 응답:

| HTTP Status | 상황 |
| --- | --- |
| `400` | 지원하지 않는 `notice_type` 요청 |
| `409` | 이미 다른 크롤링이 실행 중 |

오류 예시:

```json
{
  "detail": "이미 크롤링이 실행 중입니다."
}
```

### 4. Cafeteria Crawl Status

- `GET /cafeteria-crawl/status`: 현재 프로세스의 최근 실행 상태. 재시작 시 초기화됩니다.
- `run_id`, `status`, `started_at`, `finished_at`, `business_date`, `only_pending`, `results`를 반환합니다.
- `results`는 식당 타입을 키로 하는 객체이며 각 결과는 `status`, `url`, `saved_daily_menus`, `updated`, `error`, `menus`를 가집니다.
- 전체 상태: `idle`, `starting`, `running`, `completed`, `unchanged`, `partial_failed`, `failed`.
- 식당 상태: `pending`, `running`, `completed`, `unchanged`, `skipped`, `failed`.
- `retry_count=0`, `max_retry_count=0`, `next_retry_at=null`은 호환용입니다. 내부 재시도는 없습니다.

### 5. Trigger Cafeteria Crawl

- Method: `POST`
- Path: `/cafeteria-crawl/run`

정기 호출 본문:

```json
{"mode":"background","only_pending":true}
```

| 필드 | 타입 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `restaurant_types` | 식당 enum 배열 또는 null | null | 생략하면 전체 식당. 원본 URL은 크롤러 설정 사용 |
| `mode` | `background` 또는 `sync` | background | 비동기 접수 또는 완료 후 응답 |
| `only_pending` | boolean | false | true이면 한국 날짜 기준 당일 성공 식당을 건너뜀 |

`url`, `restaurant_type` 단수 필드는 수집 대상을 선택하지 않습니다. `restaurant_types`를 사용해야 합니다.

background 응답은 새 `run_id`와 `status=starting`을 가진 접수 스냅샷입니다. HTTP 200을 수집 완료로 취급하지 않습니다. sync는 이번 실행의 최종 상태를 반환합니다. 수집 실패는 식당별 `results`에 표시되므로 HTTP 상태만으로 성공을 판정하지 않습니다.

이번 주 식단을 정상 확인한 `completed`/`unchanged`는 당일 성공입니다. `only_pending=true`로 다시 호출하면 `skipped`가 됩니다. 다음 한국 날짜에는 자동으로 재수집 대상이 됩니다. 수동으로 다시 확인하려면 `only_pending=false`를 사용합니다.

| HTTP Status | 상황 |
| --- | --- |
| 409 | 현재 크롤러 프로세스에서 다른 식단 수집이 실행 중 |
| 400 | 요청한 식당의 URL 설정 누락 |
| 422 | 잘못된 mode, 식당 enum 또는 요청 형식 |

### 6. Persistent Daily Status

- `GET /cafeteria-crawl/daily-status`: DB에 저장된 당일 수집 결과. 서버 재시작 후에도 유지됩니다.
- 최상위: `business_date`(KST), `timezone`, `pending_restaurant_types`, `results`.
- `results[식당타입]`: `completed_today`, `status`, `business_date`, `run_id`, `last_attempt_at`, `last_success_date`, `last_success_at`, `error`.
- 기록이 없는 식당: `status=pending`, `completed_today=false`, 날짜/시각/run_id/error는 null.
- `status`는 마지막 시도의 상태이고 `completed_today`가 당일 성공 판정값입니다. 어제 completed였어도 오늘은 false입니다.
- 시각은 UTC ISO 8601, 날짜는 한국 시간 기준입니다. 시작 날짜로 작업을 묶어 자정을 넘긴 어제 작업을 오늘 성공으로 계산하지 않습니다.
- 기록 DB를 읽을 수 없으면 503을 반환합니다.

정기 호출은 Spring에서 `0 0 1-17/2 * * MON-FRI`, `Asia/Seoul`로 관리하고 항상 `only_pending=true`로 요청합니다. Python은 시간 제한이나 자동 기동을 하지 않으므로 Spring 일정 배포가 필수입니다. 17시에 시작한 작업은 완료까지 진행합니다. 01시에 성공한 뒤 학교가 수정한 내용은 다음 평일 01시에 확인됩니다.

식당별 수집 이력 테이블은 `python -m crawler.cafeteria.migrate`로 준비합니다. `scripts/deploy.sh`가 재시작 전에 자동 실행합니다. 현재 실행 잠금은 프로세스 내부이므로 크롤러는 단일 worker/인스턴스로 운영합니다.

Spring 작업 프롬프트: [SPRING_CAFETERIA_HANDOFF.md](SPRING_CAFETERIA_HANDOFF.md)

## How Spring Boot Should Use The API

권장 사용 방식:

1. 운영 배치 또는 관리자 기능에서 `POST /crawl/run` 호출
2. 비동기 실행을 원하면 `mode=background` 사용
3. 이후 `GET /crawl/status` 폴링으로 상태 확인
4. 식단 수집은 `POST /cafeteria-crawl/run` 호출
5. 이후 `GET /cafeteria-crawl/status` 폴링으로 상태 확인
6. 실제 공지/식단 데이터 조회는 Spring Boot가 MySQL에서 직접 조회

Spring Boot에서 Python API를 호출하는 용도:

- 관리자 수동 수집 버튼
- 스케줄 실행 트리거
- 마지막 수집 상태 모니터링
- 식단 크롤링 수동 실행
- 식단 마지막 수집 상태 모니터링

Spring Boot에서 Python API를 호출하지 않는 용도:

- 공지 목록 조회
- 공지 상세 조회
- 첨부파일 목록 조회
- 식단 조회

위 항목들은 전부 MySQL 직접 조회가 맞습니다.

## Example cURL

### Health Check

```bash
curl http://34.64.250.12:8000/health
```

### 전체 크롤링 비동기 실행

```bash
curl -X POST http://34.64.250.12:8000/crawl/run \
  -H "Content-Type: application/json" \
  -d '{"mode":"background"}'
```

### 학사공지와 장학공지 동기 실행

```bash
curl -X POST http://34.64.250.12:8000/crawl/run \
  -H "Content-Type: application/json" \
  -d '{"notice_types":["academic","scholarship"],"mode":"sync"}'
```

### 상태 확인

```bash
curl http://34.64.250.12:8000/crawl/status
```

### 식단 크롤링 비동기 실행

```bash
curl -X POST http://34.64.250.12:8000/cafeteria-crawl/run \
  -H "Content-Type: application/json" \
  -d '{"mode":"background","only_pending":true}'
```

### 식단 상태 확인

```bash
curl http://34.64.250.12:8000/cafeteria-crawl/status
```

## Python API Run Command

운영 API 서버 실행 명령:

```bash
python -m uvicorn api_main:app --host 0.0.0.0 --port 8000
```

CLI 단독 실행 명령:

```bash
python main.py
```

## Spring Boot Mapping Recommendation

Java 엔티티/DTO 권장 필드명:

### Notice

- `id`
- `noticeType`
- `originNoticeId`
- `title`
- `sourceUrl`
- `contentHtml`
- `changeSummary`
- `author`
- `postDate`
- `hot`
- `attachments`

### NoticeFile

- `id`
- `noticeId`
- `fileName`
- `fileUrl`

## Final Notes

- Python API는 크롤링 실행 제어 전용입니다.
- Spring Boot는 공지/식단 조회 API를 자체적으로 구현하는 방향이 맞습니다.
- `content_html`은 이미 렌더 가능한 HTML이므로, Spring Boot에서는 그대로 내려주되 XSS 정책은 서비스 정책에 맞게 검토해야 합니다.
- 이미지 URL은 `http://34.64.250.12/images/...` 형식으로 저장됩니다.
- 원본 공지 링크는 `source_url` 컬럼에 저장됩니다.
- 식단 데이터는 `daily_menus` -> `meal_sections` -> `dishes` 구조로 저장됩니다.
