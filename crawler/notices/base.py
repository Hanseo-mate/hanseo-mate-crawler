import logging

from bs4 import BeautifulSoup, Tag

from ..config import BASE_URL
from ..database import (
    delete_expired_notices,
    ensure_schema,
    get_db_connection,
    get_existing_notice_summaries,
    get_retention_cutoff_date,
    insert_notice_if_absent,
    sync_notice,
)
from ..html_utils import (
    extract_notice_title,
    extract_onclick_args,
    extract_origin_notice_id,
    extract_view_box_html_raw,
    is_hot_notice,
    normalize_text,
    parse_attachments,
    parse_iso_date,
    parse_metadata_value,
    process_embedded_images,
)
from ..http import create_session, fetch_document
from ..models import BoardDefinition, CrawlStats, NoticeRecord, NoticeSummary


class HanseoNoticeCrawler:
    def __init__(self, board: BoardDefinition) -> None:
        self.board = board

    @staticmethod
    def should_collect_notice(summary: NoticeSummary, retention_cutoff_date) -> bool:
        return summary.is_hot or summary.post_date >= retention_cutoff_date

    def build_list_page_url(self, page_num: int) -> str:
        return (
            f"{BASE_URL}/boardCnts/list.do"
            f"?boardID={self.board.board_id}&m={self.board.menu_code}"
            f"&s={self.board.site_code}&page={page_num}"
        )

    def build_detail_page_url(
        self,
        origin_notice_id: str,
        page_num: int,
        lev: str,
        status_yn: str,
    ) -> str:
        return (
            f"{BASE_URL}/boardCnts/view.do"
            f"?boardID={self.board.board_id}&boardSeq={origin_notice_id}&lev={lev}"
            f"&searchType=null&statusYN={status_yn}&page={page_num}"
            f"&s={self.board.site_code}&m={self.board.menu_code}"
        )

    def parse_list_page(self, soup: BeautifulSoup, page_num: int) -> list[NoticeSummary]:
        table_body = soup.select_one("table.wb tbody")
        if table_body is None:
            raise ValueError(f"{self.board.name} 페이지 {page_num}: 목록 테이블을 찾을 수 없습니다.")

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
                raise ValueError(
                    f"{self.board.name} 페이지 {page_num}: onclick 파라미터 파싱 실패 - {onclick_value}"
                )

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

    def parse_detail_page(
        self,
        document,
        summary: NoticeSummary,
        source_url: str,
    ) -> NoticeRecord:
        soup = document.soup
        article = soup.select_one("article.board-text")
        if article is None:
            raise ValueError(f"공지 {summary.origin_notice_id}: 상세 article을 찾을 수 없습니다.")

        title_element = article.select_one("h1.tit")
        info_items = article.select("ul.infoBox li")
        view_box = article.select_one("div.viewBox")

        if view_box is None:
            raise ValueError(f"공지 {summary.origin_notice_id}: 본문 viewBox를 찾을 수 없습니다.")

        parsed_content_html = view_box.decode_contents().strip()
        raw_content_html = extract_view_box_html_raw(document.html_text)
        if raw_content_html is not None and raw_content_html.strip():
            content_html = raw_content_html.strip()
        else:
            content_html = parsed_content_html

        logging.debug(
            "본문 추출: notice_type=%s, origin_notice_id=%s, raw_length=%s, parsed_length=%s, saved_length=%s",
            self.board.key,
            summary.origin_notice_id,
            len(raw_content_html.strip()) if raw_content_html and raw_content_html.strip() else -1,
            len(parsed_content_html),
            len(content_html),
        )
        if not content_html:
            raise ValueError(f"공지 {summary.origin_notice_id}: 본문 HTML이 비어 있습니다.")

        content_html = process_embedded_images(content_html)

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
            notice_type=self.board.key,
            origin_notice_id=summary.origin_notice_id,
            title=title,
            source_url=source_url,
            content_html=content_html,
            author=author,
            post_date=post_date,
            is_hot=summary.is_hot,
            attachments=attachments,
        )

    def fetch_notice_detail(self, session, summary: NoticeSummary) -> NoticeRecord:
        detail_url = self.build_detail_page_url(
            origin_notice_id=summary.origin_notice_id,
            page_num=summary.page_num,
            lev=summary.lev,
            status_yn=summary.status_yn,
        )
        logging.info("%s 상세 요청: %s", self.board.name, detail_url)
        detail_document = fetch_document(session, detail_url)
        return self.parse_detail_page(detail_document, summary, detail_url)

    def crawl_and_sync_notices(self) -> CrawlStats:
        stats = CrawlStats()
        retention_cutoff_date = get_retention_cutoff_date()

        with create_session() as session:
            with get_db_connection() as connection:
                ensure_schema(connection)

                for page_num in range(self.board.page_count, 0, -1):
                    page_url = self.build_list_page_url(page_num)
                    logging.info("%s 목록 요청: %s", self.board.name, page_url)

                    try:
                        list_document = fetch_document(session, page_url)
                        parsed_notices = self.parse_list_page(list_document.soup, page_num)
                        notices = [
                            summary
                            for summary in parsed_notices
                            if self.should_collect_notice(summary, retention_cutoff_date)
                        ]
                        existing_notice_summaries = get_existing_notice_summaries(
                            connection,
                            self.board.key,
                            [summary.origin_notice_id for summary in notices],
                        )
                        stats.pages_processed += 1
                        if not notices:
                            logging.debug(
                                "%s 페이지 %s: 컷오프 이전 비HOT 공지만 있어 상세 크롤링을 건너뜀",
                                self.board.name,
                                page_num,
                            )
                    except Exception as exc:
                        logging.exception("%s 페이지 %s 처리 실패: %s", self.board.name, page_num, exc)
                        continue

                    for summary in notices:
                        existing_notice = existing_notice_summaries.get(summary.origin_notice_id)
                        if existing_notice is not None and (
                            existing_notice["title"] == summary.title
                            and existing_notice["author"] == summary.author
                            and existing_notice["post_date"] == summary.post_date
                            and existing_notice["is_hot"] == summary.is_hot
                        ):
                            logging.debug(
                                "변경 없는 기존 공지 건너뜀: notice_type=%s, origin_notice_id=%s",
                                self.board.key,
                                summary.origin_notice_id,
                            )
                            continue

                        try:
                            if existing_notice is None:
                                notice = self.fetch_notice_detail(session, summary)
                                notice_id, inserted = insert_notice_if_absent(connection, notice)
                                if not inserted:
                                    logging.debug(
                                        "중복 공지 저장 건너뜀: notice_type=%s, notice_id=%s, origin_notice_id=%s",
                                        self.board.key,
                                        notice_id,
                                        summary.origin_notice_id,
                                    )
                                    continue

                                stats.notices_inserted += 1
                                existing_notice_summaries[notice.origin_notice_id] = {
                                    "id": notice_id,
                                    "title": notice.title,
                                    "author": notice.author,
                                    "post_date": notice.post_date,
                                    "is_hot": notice.is_hot,
                                }
                                logging.info(
                                    "신규 공지 저장 완료: notice_type=%s, notice_id=%s, origin_notice_id=%s, attachments=%s, post_date=%s",
                                    notice.notice_type,
                                    notice_id,
                                    notice.origin_notice_id,
                                    len(notice.attachments),
                                    notice.post_date,
                                )
                            else:
                                notice = self.fetch_notice_detail(session, summary)
                                notice_id, sync_status = sync_notice(connection, notice)
                                if sync_status == "updated":
                                    stats.notices_updated += 1
                                    existing_notice_summaries[notice.origin_notice_id] = {
                                        "id": notice_id,
                                        "title": notice.title,
                                        "author": notice.author,
                                        "post_date": notice.post_date,
                                        "is_hot": notice.is_hot,
                                    }
                                    logging.info(
                                        "기존 공지 갱신 완료: notice_type=%s, notice_id=%s, origin_notice_id=%s, attachments=%s, post_date=%s",
                                        notice.notice_type,
                                        notice_id,
                                        notice.origin_notice_id,
                                        len(notice.attachments),
                                        notice.post_date,
                                    )
                                else:
                                    logging.debug(
                                        "기존 공지 변경 없음: notice_type=%s, notice_id=%s, origin_notice_id=%s",
                                        self.board.key,
                                        notice_id,
                                        summary.origin_notice_id,
                                    )
                        except Exception as exc:
                            stats.notices_failed += 1
                            logging.exception(
                                "%s 공지 %s 처리 실패: %s",
                                self.board.name,
                                summary.origin_notice_id,
                                exc,
                            )
                            continue

                stats.notices_deleted = delete_expired_notices(connection, self.board.key)
                logging.info("%s 1년 초과 데이터 삭제 건수: %s", self.board.name, stats.notices_deleted)

        return stats
