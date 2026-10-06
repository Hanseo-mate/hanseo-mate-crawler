from dataclasses import dataclass
from datetime import date
from enum import Enum

from bs4 import BeautifulSoup
from sqlalchemy import Boolean, Column, Date, DateTime, Enum as SqlEnum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class MealTime(str, Enum):
    LUNCH = "LUNCH"
    DINNER = "DINNER"


class RestaurantType(str, Enum):
    MAIN_STUDENT = "MAIN_STUDENT"
    MAIN_STAFF = "MAIN_STAFF"
    TAEAN_STUDENT = "TAEAN_STUDENT"
    TAEAN_STAFF = "TAEAN_STAFF"


class DailyMenu(Base):
    __tablename__ = "daily_menus"
    __table_args__ = (UniqueConstraint("restaurant_type", "menu_date", name="uk_daily_menu_restaurant_date"),)

    id = Column(Integer, primary_key=True)
    restaurant_type = Column(SqlEnum(RestaurantType), nullable=False)
    menu_date = Column(Date, nullable=False)
    meal_sections = relationship(
        "MealSection",
        back_populates="daily_menu",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MealSection(Base):
    __tablename__ = "meal_sections"

    id = Column(Integer, primary_key=True)
    daily_menu_id = Column(Integer, ForeignKey("daily_menus.id", ondelete="CASCADE"), nullable=False)
    meal_time = Column(SqlEnum(MealTime), nullable=False)
    corner_name = Column(String(100), nullable=False)
    price = Column(Integer, nullable=True)
    dishes = Column(JSON, nullable=False)
    raw_text = Column(Text, nullable=False)
    daily_menu = relationship("DailyMenu", back_populates="meal_sections")


class CafeteriaCrawlProgress(Base):
    """식당별 마지막 시도와 성공 기록. 날짜는 KST, 시각은 UTC로 저장한다."""

    __tablename__ = "cafeteria_crawl_progress"

    restaurant_type = Column(String(40), primary_key=True)
    business_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False)
    run_id = Column(String(32), nullable=False)
    last_attempt_at = Column(DateTime, nullable=False)
    last_success_date = Column(Date, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)


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
