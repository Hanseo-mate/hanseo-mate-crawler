import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

import pymysql
import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://www.hanseo.ac.kr"
LIST_URL_TEMPLATE = (
    "https://www.hanseo.ac.kr/boardCnts/list.do"
    "?boardID=298&m=040101&s=hs&page={page_num}"
)
DETAIL_URL_TEMPLATE = (
    "https://www.hanseo.ac.kr/boardCnts/view.do"
    "?boardID=298&boardSeq={origin_notice_id}&lev={lev}"
    "&searchType=null&statusYN={status_yn}&page={page_num}&s=hs&m=040101"
)
REQUEST_DELAY_SECONDS = 1
REQUEST_TIMEOUT = (10, 30)

DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "root",
    "password": "1234",
    "database": "hanseo_mate",
    "charset": "utf8mb4",
    "autocommit": False,
}

CREATE_NOTICES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    origin_notice_id VARCHAR(32) NOT NULL,
    title VARCHAR(500) NOT NULL,
    content_html LONGTEXT NOT NULL,
    author VARCHAR(100) NOT NULL,
    post_date DATE NOT NULL,
    is_hot BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uk_notices_origin_notice_id (origin_notice_id),
    KEY idx_notices_post_date (post_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

ALTER_NOTICES_CONTENT_HTML_SQL = """
ALTER TABLE notices
MODIFY content_html LONGTEXT NOT NULL;
"""

CREATE_NOTICE_FILES_TABLE_SQL = """
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
"""

UPSERT_NOTICE_SQL = """
INSERT INTO notices (
    origin_notice_id,
    title,
    content_html,
    author,
    post_date,
    is_hot
) VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    title = VALUES(title),
    content_html = VALUES(content_html),
    author = VALUES(author),
    post_date = VALUES(post_date),
    is_hot = VALUES(is_hot);
"""

SELECT_NOTICE_ID_SQL = """
SELECT id
FROM notices
WHERE origin_notice_id = %s;
"""

DELETE_NOTICE_FILES_SQL = """
DELETE FROM notice_files
WHERE notice_id = %s;
"""

INSERT_NOTICE_FILES_SQL = """
INSERT INTO notice_files (
    notice_id,
    file_name,
    file_url
) VALUES (%s, %s, %s);
"""

DELETE_EXPIRED_NOTICES_SQL = """
DELETE FROM notices
WHERE post_date <= DATE_SUB(CURDATE(), INTERVAL 2 YEAR);
"""

ORIGIN_NOTICE_ID_PATTERN = re.compile(r"goView\('(?:[^']*)','([^']+)'")


@dataclass(frozen=True)
class NoticeSummary:
    origin_notice_id: str
    title: str
    author: str
    post_date: date
    is_hot: bool
    page_num: int
    lev: str
    status_yn: str


@dataclass(frozen=True)
class NoticeRecord:
    origin_notice_id: str
    title: str
    content_html: str
    author: str
    post_date: date
    is_hot: bool
    attachments: list[dict[str, str]]


@dataclass
class CrawlStats:
    pages_processed: int = 0
    notices_upserted: int = 0
    notices_failed: int = 0
    notices_deleted: int = 0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def get_db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(**DB_CONFIG)


def ensure_schema(connection: pymysql.connections.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CREATE_NOTICES_TABLE_SQL)
        cursor.execute(ALTER_NOTICES_CONTENT_HTML_SQL)
        cursor.execute(CREATE_NOTICE_FILES_TABLE_SQL)
    connection.commit()


def fetch_document(session: requests.Session, url: str) -> BeautifulSoup:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return BeautifulSoup(response.text, "html.parser")
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)


def build_list_page_url(page_num: int) -> str:
    return LIST_URL_TEMPLATE.format(page_num=page_num)


def build_detail_page_url(
    origin_notice_id: str,
    page_num: int,
    lev: str,
    status_yn: str,
) -> str:
    return DETAIL_URL_TEMPLATE.format(
        origin_notice_id=origin_notice_id,
        page_num=page_num,
        lev=lev,
        status_yn=status_yn,
    )


def parse_iso_date(raw_value: str) -> date:
    return datetime.strptime(raw_value.strip(), "%Y-%m-%d").date()


def normalize_text(raw_text: str) -> str:
    return " ".join(raw_text.split())


def extract_onclick_args(onclick_value: str) -> list[str]:
    return re.findall(r"'([^']*)'", onclick_value)


def extract_origin_notice_id(onclick_value: str) -> str:
    match = ORIGIN_NOTICE_ID_PATTERN.search(onclick_value)
    if not match:
        raise ValueError(f"origin_notice_id 추출 실패: {onclick_value}")
    return match.group(1)


def is_hot_notice(number_cell: Tag) -> bool:
    hot_icon = number_cell.find("img", alt=re.compile(r"HOT"))
    return hot_icon is not None


def extract_notice_title(anchor: Tag) -> str:
    title_from_attribute = anchor.get("title", "").strip()
    if title_from_attribute:
        return normalize_text(title_from_attribute)
    return normalize_text(anchor.get_text(" ", strip=True))


def parse_list_page(soup: BeautifulSoup, page_num: int) -> list[NoticeSummary]:
    table_body = soup.select_one("table.wb tbody")
    if table_body is None:
        raise ValueError(f"페이지 {page_num}: 목록 테이블을 찾을 수 없습니다.")

    rows = table_body.find_all("tr", recursive=False)
    rows.reverse()

    notices: list[NoticeSummary] = []
    for row in rows:
        columns = row.find_all("td", recursive=False)
        if len(columns) < 5:
            continue

        link = columns[1].find("a", onclick=True)
        if link is None:
            continue

        onclick_value = link["onclick"]
        onclick_args = extract_onclick_args(onclick_value)
        if len(onclick_args) < 6:
            raise ValueError(f"페이지 {page_num}: onclick 파라미터 파싱 실패 - {onclick_value}")

        origin_notice_id = extract_origin_notice_id(onclick_value)
        notice = NoticeSummary(
            origin_notice_id=origin_notice_id,
            title=extract_notice_title(link),
            author=normalize_text(columns[2].get_text(" ", strip=True)),
            post_date=parse_iso_date(columns[4].get_text(strip=True)),
            is_hot=is_hot_notice(columns[0]),
            page_num=page_num,
            lev=onclick_args[2] or "0",
            status_yn=onclick_args[4] or "W",
        )
        notices.append(notice)

    return notices


def remove_unwanted_tags(container: Tag) -> None:
    for tag in container.select("script, style, noscript"):
        tag.decompose()


def parse_metadata_value(info_items: list[Tag], label: str) -> Optional[str]:
    for item in info_items:
        strong = item.find("strong")
        if strong is None:
            continue

        normalized_label = normalize_text(strong.get_text(strip=True))
        if normalized_label != label:
            continue

        strong.extract()
        return normalize_text(item.get_text(" ", strip=True))
    return None


def parse_attachments(article: Tag) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []

    for anchor in article.select("div.fieldBox dd a[href]"):
        href = anchor.get("href", "").strip()
        if not href or "synapView" in href:
            continue

        preview_icon = anchor.find(
            "img",
            src=lambda value: value is not None and "/images/board/btn_preview.gif" in value,
        )
        if preview_icon is not None:
            continue

        file_name = normalize_text(anchor.get_text(" ", strip=True))
        if not file_name:
            continue

        attachments.append(
            {
                "file_name": file_name,
                "file_url": href,
            }
        )

    return attachments


def parse_detail_page(soup: BeautifulSoup, summary: NoticeSummary) -> NoticeRecord:
    article = soup.select_one("article.board-text")
    if article is None:
        raise ValueError(f"공지 {summary.origin_notice_id}: 상세 article을 찾을 수 없습니다.")

    title_element = article.select_one("h1.tit")
    info_items = article.select("ul.infoBox li")
    view_box = article.select_one("div.viewBox")

    if view_box is None:
        raise ValueError(f"공지 {summary.origin_notice_id}: 본문 viewBox를 찾을 수 없습니다.")

    remove_unwanted_tags(view_box)
    content_html = view_box.decode_contents().strip()
    if not content_html:
        raise ValueError(f"공지 {summary.origin_notice_id}: 본문 HTML이 비어 있습니다.")

    author = parse_metadata_value(info_items, "작성자") or summary.author
    post_date_text = parse_metadata_value(info_items, "작성일")
    post_date = parse_iso_date(post_date_text) if post_date_text else summary.post_date
    title = (
        normalize_text(title_element.get_text(" ", strip=True))
        if title_element is not None
        else summary.title
    )
    attachments = parse_attachments(article)

    return NoticeRecord(
        origin_notice_id=summary.origin_notice_id,
        title=title,
        content_html=content_html,
        author=author,
        post_date=post_date,
        is_hot=summary.is_hot,
        attachments=attachments,
    )


def sync_notice(
    connection: pymysql.connections.Connection,
    notice: NoticeRecord,
) -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                UPSERT_NOTICE_SQL,
                (
                    notice.origin_notice_id,
                    notice.title,
                    notice.content_html,
                    notice.author,
                    notice.post_date,
                    notice.is_hot,
                ),
            )
            cursor.execute(SELECT_NOTICE_ID_SQL, (notice.origin_notice_id,))
            notice_row = cursor.fetchone()
            if notice_row is None:
                raise ValueError(
                    f"공지 {notice.origin_notice_id}: upsert 후 notice_id를 조회할 수 없습니다."
                )

            notice_id = notice_row[0]
            cursor.execute(DELETE_NOTICE_FILES_SQL, (notice_id,))

            if notice.attachments:
                cursor.executemany(
                    INSERT_NOTICE_FILES_SQL,
                    [
                        (notice_id, attachment["file_name"], attachment["file_url"])
                        for attachment in notice.attachments
                    ],
                )

        connection.commit()
        return notice_id
    except Exception:
        connection.rollback()
        raise


def delete_expired_notices(connection: pymysql.connections.Connection) -> int:
    with connection.cursor() as cursor:
        affected_rows = cursor.execute(DELETE_EXPIRED_NOTICES_SQL)
    connection.commit()
    return affected_rows


def fetch_notice_detail(
    session: requests.Session,
    summary: NoticeSummary,
) -> NoticeRecord:
    detail_url = build_detail_page_url(
        origin_notice_id=summary.origin_notice_id,
        page_num=summary.page_num,
        lev=summary.lev,
        status_yn=summary.status_yn,
    )
    logging.info("상세 요청: %s", urljoin(BASE_URL, detail_url))
    detail_soup = fetch_document(session, detail_url)
    return parse_detail_page(detail_soup, summary)


def crawl_and_sync_notices() -> CrawlStats:
    stats = CrawlStats()

    with create_session() as session:
        with get_db_connection() as connection:
            ensure_schema(connection)

            for page_num in range(50, 0, -1):
                page_url = build_list_page_url(page_num)
                logging.info("목록 요청: %s", page_url)

                try:
                    list_soup = fetch_document(session, page_url)
                    notices = parse_list_page(list_soup, page_num)
                    stats.pages_processed += 1
                except Exception as exc:
                    logging.exception("페이지 %s 처리 실패: %s", page_num, exc)
                    continue

                for summary in notices:
                    try:
                        notice = fetch_notice_detail(session, summary)
                        notice_id = sync_notice(connection, notice)
                        stats.notices_upserted += 1
                        logging.info(
                            "동기화 완료: notice_id=%s, origin_notice_id=%s, attachments=%s, post_date=%s",
                            notice_id,
                            notice.origin_notice_id,
                            len(notice.attachments),
                            notice.post_date,
                        )
                    except Exception as exc:
                        stats.notices_failed += 1
                        logging.exception(
                            "공지 %s 처리 실패: %s",
                            summary.origin_notice_id,
                            exc,
                        )
                        continue

            stats.notices_deleted = delete_expired_notices(connection)
            logging.info("2년 초과 데이터 삭제 건수: %s", stats.notices_deleted)

    return stats


def main() -> None:
    configure_logging()

    try:
        stats = crawl_and_sync_notices()
        logging.info(
            "크롤링 완료 - pages_processed=%s, notices_upserted=%s, notices_failed=%s, notices_deleted=%s",
            stats.pages_processed,
            stats.notices_upserted,
            stats.notices_failed,
            stats.notices_deleted,
        )
    except Exception as exc:
        logging.exception("크롤러 실행 실패: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
