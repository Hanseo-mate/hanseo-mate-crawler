import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from sqlalchemy.orm import Session, selectinload

from ..config import REQUEST_TIMEOUT
from ..database import ensure_cafeteria_schema, get_db_session
from ..models import DailyMenu, RestaurantType
from .parser import parse_cafeteria_menu


RETRY_DELAY = timedelta(hours=2)
MAX_RETRY_COUNT = 5


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_daily_menus(menus: list[DailyMenu]) -> list[dict[str, Any]]:
    return [
        {
            "menuDate": menu.menu_date.isoformat(),
            "restaurantType": menu.restaurant_type.value,
            "mealSections": [
                {
                    "mealTime": section.meal_time.value,
                    "cornerName": section.corner_name,
                    "price": section.price,
                    "dishes": list(section.dishes),
                    "rawText": section.raw_text,
                }
                for section in menu.meal_sections
            ],
        }
        for menu in menus
    ]


@dataclass
class CafeteriaRunState:
    run_id: str | None = None
    status: str = "idle"
    requested_url: str | None = None
    requested_restaurant_type: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    saved_daily_menus: int = 0
    updated: bool | None = None
    retry_count: int = 0
    max_retry_count: int = MAX_RETRY_COUNT
    next_retry_at: str | None = None
    menus: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class CafeteriaSyncResult:
    menus: list[DailyMenu]
    updated: bool


def _menu_content(menus: list[DailyMenu]) -> tuple:
    return tuple(
        (
            menu.menu_date.isoformat(),
            menu.restaurant_type.value,
            tuple(
                (
                    section.meal_time.value,
                    section.corner_name,
                    section.price,
                    tuple(section.dishes),
                    section.raw_text,
                )
                for section in menu.meal_sections
            ),
        )
        for menu in sorted(menus, key=lambda item: item.menu_date)
    )


def crawl_and_save_cafeteria(
    url: str,
    rest_type: RestaurantType,
    db_session: Session,
) -> CafeteriaSyncResult:
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    menus = parse_cafeteria_menu(response.text, rest_type)
    if not menus:
        raise ValueError("크롤링 결과에 유효한 식단이 없습니다. 기존 데이터는 유지합니다.")

    menu_dates = [menu.menu_date for menu in menus]
    existing_menus = (
        db_session.query(DailyMenu)
        .options(selectinload(DailyMenu.meal_sections))
        .filter(
            DailyMenu.restaurant_type == rest_type,
            DailyMenu.menu_date.in_(menu_dates),
        )
        .order_by(DailyMenu.menu_date)
        .all()
    )
    if _menu_content(existing_menus) == _menu_content(menus):
        return CafeteriaSyncResult(menus=existing_menus, updated=False)

    db_session.query(DailyMenu).filter(DailyMenu.restaurant_type == rest_type).delete(
        synchronize_session=False
    )
    db_session.add_all(menus)
    db_session.commit()
    return CafeteriaSyncResult(menus=menus, updated=True)


class CafeteriaCrawlService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = CafeteriaRunState()
        self._retry_timer: threading.Timer | None = None
        self._background_starting = False

    def get_state(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def run_crawler(
        self,
        url: str,
        rest_type: RestaurantType,
        retry_count: int = 0,
        _preclaimed: bool = False,
    ) -> list[DailyMenu]:
        run_id = uuid.uuid4().hex

        with self._lock:
            if self._state.status == "running" or (self._background_starting and not _preclaimed):
                raise RuntimeError("이미 식단 크롤링이 실행 중입니다.")
            self._background_starting = False
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None

            self._state = CafeteriaRunState(
                run_id=run_id,
                status="running",
                requested_url=url,
                requested_restaurant_type=rest_type.value,
                started_at=utc_now_iso(),
                saved_daily_menus=0,
                retry_count=retry_count,
                error=None,
            )

        logging.info("식단 크롤링 시작: run_id=%s, restaurant_type=%s, url=%s", run_id, rest_type.value, url)

        try:
            ensure_cafeteria_schema()
            with get_db_session() as db_session:
                result = crawl_and_save_cafeteria(url, rest_type, db_session)

            with self._lock:
                self._state.status = "completed" if result.updated else "unchanged"
                self._state.finished_at = utc_now_iso()
                self._state.saved_daily_menus = len(result.menus) if result.updated else 0
                self._state.updated = result.updated
                self._state.menus = serialize_daily_menus(result.menus)

            if result.updated:
                self._cancel_retry()
            elif retry_count < MAX_RETRY_COUNT:
                self._schedule_retry(url, rest_type, retry_count + 1)

            return result.menus
        except Exception as exc:
            logging.exception("식단 크롤링 실패: run_id=%s, error=%s", run_id, exc)
            with self._lock:
                self._state.status = "failed"
                self._state.finished_at = utc_now_iso()
                self._state.error = str(exc)
            raise

    def _cancel_retry(self) -> None:
        with self._lock:
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
            self._state.next_retry_at = None

    def _schedule_retry(self, url: str, rest_type: RestaurantType, retry_count: int) -> None:
        next_retry = datetime.now(timezone.utc) + RETRY_DELAY

        def retry() -> None:
            try:
                self.run_crawler(url, rest_type, retry_count)
            except Exception:
                logging.exception("식단 크롤링 재시도 실패: retry_count=%s", retry_count)

        timer = threading.Timer(RETRY_DELAY.total_seconds(), retry)
        timer.name = f"cafeteria-crawler-retry-{retry_count}"
        timer.daemon = True
        with self._lock:
            if self._retry_timer is not None:
                self._retry_timer.cancel()
            self._retry_timer = timer
            self._state.next_retry_at = next_retry.isoformat()
        timer.start()

    def start_background_run(self, url: str, rest_type: RestaurantType) -> dict:
        with self._lock:
            if self._state.status == "running" or self._background_starting:
                raise RuntimeError("이미 식단 크롤링이 실행 중입니다.")
            self._background_starting = True
            self._state.status = "starting"

        def run() -> None:
            try:
                self.run_crawler(url, rest_type, _preclaimed=True)
            except Exception:
                logging.exception("백그라운드 식단 크롤링 실패")

        thread = threading.Thread(target=run, name="cafeteria-crawler-runner", daemon=True)
        thread.start()
        return self.get_state()


cafeteria_crawl_service = CafeteriaCrawlService()