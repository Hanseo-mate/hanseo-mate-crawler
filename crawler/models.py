from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class BoardDefinition:
    key: str
    name: str
    board_id: int
    menu_code: str
    site_code: str = "hs"
    page_count: int = 50


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
    notice_type: str
    origin_notice_id: str
    title: str
    source_url: str
    content_html: str
    author: str
    post_date: date
    is_hot: bool
    attachments: list[dict[str, str]]


@dataclass
class CrawlStats:
    pages_processed: int = 0
    notices_inserted: int = 0
    notices_updated: int = 0
    notices_failed: int = 0
    notices_deleted: int = 0


@dataclass(frozen=True)
class FetchedDocument:
    soup: BeautifulSoup
    html_text: str
