import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

from ..config import REQUEST_TIMEOUT
from ..database import ensure_cafeteria_schema, get_db_session
from ..models import DailyMenu, RestaurantType
from .parser import parse_cafeteria_menu


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CafeteriaRunState:
    run_id: str | None = None
    status: str = "idle"
    requested_url: str | None = None
    requested_restaurant_type: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    saved_daily_menus: int = 0
    error: str | None = None


def crawl_and_save_cafeteria(
    url: str,
    rest_type: RestaurantType,
    db_session: Session,
) -> list[DailyMenu]:
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    menus = parse_cafeteria_menu(response.text, rest_type)
    if not menus:
        db_session.commit()
        return []

    existing_menus = (
        db_session.query(DailyMenu)
        .filter(
            DailyMenu.restaurant_type == rest_type,
            DailyMenu.menu_date.in_([menu.menu_date for menu in menus]),
        )
        .all()
    )
    for existing_menu in existing_menus:
        db_session.delete(existing_menu)

    db_session.add_all(menus)
    db_session.commit()
    return menus


class CafeteriaCrawlService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = CafeteriaRunState()

    def get_state(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def run_crawler(self, url: str, rest_type: RestaurantType) -> list[DailyMenu]:
        run_id = uuid.uuid4().hex

        with self._lock:
            if self._state.status == "running":
                raise RuntimeError("이미 식단 크롤링이 실행 중입니다.")

            self._state = CafeteriaRunState(
                run_id=run_id,
                status="running",
                requested_url=url,
                requested_restaurant_type=rest_type.value,
                started_at=utc_now_iso(),
                saved_daily_menus=0,
                error=None,
            )

        logging.info("식단 크롤링 시작: run_id=%s, restaurant_type=%s, url=%s", run_id, rest_type.value, url)

        try:
            ensure_cafeteria_schema()
            with get_db_session() as db_session:
                menus = crawl_and_save_cafeteria(url, rest_type, db_session)

            with self._lock:
                self._state.status = "completed"
                self._state.finished_at = utc_now_iso()
                self._state.saved_daily_menus = len(menus)

            return menus
        except Exception as exc:
            logging.exception("식단 크롤링 실패: run_id=%s, error=%s", run_id, exc)
            with self._lock:
                self._state.status = "failed"
                self._state.finished_at = utc_now_iso()
                self._state.error = str(exc)
            raise

    def start_background_run(self, url: str, rest_type: RestaurantType) -> dict:
        with self._lock:
            if self._state.status == "running":
                raise RuntimeError("이미 식단 크롤링이 실행 중입니다.")

        def run() -> None:
            self.run_crawler(url, rest_type)

        thread = threading.Thread(target=run, name="cafeteria-crawler-runner", daemon=True)
        thread.start()
        return self.get_state()


cafeteria_crawl_service = CafeteriaCrawlService()