from dataclasses import dataclass
from datetime import date
from enum import Enum

from bs4 import BeautifulSoup
from sqlalchemy import Boolean, Column, Date, Enum as SqlEnum, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class MealTime(str, Enum):
    LUNCH = "LUNCH"
    DINNER = "DINNER"


class MenuCategory(str, Enum):
    KOREAN = "KOREAN"
    SPECIAL = "SPECIAL"
    NORMAL = "NORMAL"


class RestaurantType(str, Enum):
    MAIN_STUDENT = "MAIN_STUDENT"
    MAIN_STAFF = "MAIN_STAFF"
    TAEAN_STUDENT = "TAEAN_STUDENT"
    TAEAN_STAFF = "TAEAN_STAFF"


class DailyMenu(Base):
    __tablename__ = "daily_menus"

    id = Column(Integer, primary_key=True)
    restaurant_type = Column(SqlEnum(RestaurantType), nullable=False)
    menu_date = Column(Date, nullable=False)
    meal_sections = relationship(
        "MealSection",
        back_populates="daily_menu",
        cascade="all, delete-orphan",
    )


class MealSection(Base):
    __tablename__ = "meal_sections"

    id = Column(Integer, primary_key=True)
    daily_menu_id = Column(Integer, ForeignKey("daily_menus.id"), nullable=False)
    meal_time = Column(SqlEnum(MealTime), nullable=False)
    menu_category = Column(SqlEnum(MenuCategory), nullable=False)
    daily_menu = relationship("DailyMenu", back_populates="meal_sections")
    dishes = relationship(
        "Dish",
        back_populates="meal_section",
        cascade="all, delete-orphan",
    )


class Dish(Base):
    __tablename__ = "dishes"

    id = Column(Integer, primary_key=True)
    meal_section_id = Column(Integer, ForeignKey("meal_sections.id"), nullable=False)
    name = Column(String(255), nullable=False)
    is_main_dish = Column(Boolean, nullable=False, default=False)
    meal_section = relationship("MealSection", back_populates="dishes")


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
