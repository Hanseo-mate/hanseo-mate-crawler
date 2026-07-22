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

## Data Behavior

### Upsert Rules

Python 크롤러는 `notices`에 대해 upsert 방식으로 동작합니다.

기준 키:

- `(notice_type, origin_notice_id)`

갱신되는 컬럼:

- `title`
- `source_url`
- `content_html`
- `author`
- `post_date`
- `is_hot`

첨부파일은 공지별로 기존 데이터를 삭제한 뒤 다시 insert 합니다.

### Expired Data Deletion

크롤링 실행 마지막 단계에서 아래 조건의 데이터가 삭제됩니다.

```sql
DELETE FROM notices
WHERE post_date <= DATE_SUB(CURDATE(), INTERVAL 2 YEAR);
```

`notice_files`는 FK `ON DELETE CASCADE`로 함께 삭제됩니다.

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
    "notices_upserted": 734,
    "notices_failed": 3,
    "notices_deleted": 18
  },
  "general": {
    "pages_processed": 50,
    "notices_upserted": 680,
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
      "notices_upserted": 734,
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

## How Spring Boot Should Use The API

권장 사용 방식:

1. 운영 배치 또는 관리자 기능에서 `POST /crawl/run` 호출
2. 비동기 실행을 원하면 `mode=background` 사용
3. 이후 `GET /crawl/status` 폴링으로 상태 확인
4. 실제 공지 데이터 조회는 Spring Boot가 MySQL에서 직접 조회

Spring Boot에서 Python API를 호출하는 용도:

- 관리자 수동 수집 버튼
- 스케줄 실행 트리거
- 마지막 수집 상태 모니터링

Spring Boot에서 Python API를 호출하지 않는 용도:

- 공지 목록 조회
- 공지 상세 조회
- 첨부파일 목록 조회

위 3가지는 전부 MySQL 직접 조회가 맞습니다.

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
- Spring Boot는 조회 전용 API를 자체적으로 구현하는 방향이 맞습니다.
- `content_html`은 이미 렌더 가능한 HTML이므로, Spring Boot에서는 그대로 내려주되 XSS 정책은 서비스 정책에 맞게 검토해야 합니다.
- 이미지 URL은 `http://34.64.250.12/images/...` 형식으로 저장됩니다.
- 원본 공지 링크는 `source_url` 컬럼에 저장됩니다.
