import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session, selectinload

from ..config import CAFETERIA_URLS, REQUEST_TIMEOUT
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
class RestaurantResult:
    """식당 하나의 크롤링 결과"""
    status: str = "pending"          # pending | running | completed | unchanged | failed
    url: str | None = None
    saved_daily_menus: int = 0
    updated: bool | None = None
    error: str | None = None
    menus: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CafeteriaRunState:
    run_id: str | None = None
    status: str = "idle"             # idle | running | completed | partial_failed | failed
    started_at: str | None = None
    finished_at: str | None = None
    retry_count: int = 0
    max_retry_count: int = MAX_RETRY_COUNT
    next_retry_at: str | None = None
    results: dict[str, RestaurantResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # results 안의 RestaurantResult도 dict로 직렬화됨 (asdict가 자동 처리)
        return d


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


def _validate_url(url: str) -> None:
    """URL이 유효한 http/https 형식인지 검증합니다."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError(f"올바르지 않은 URL입니다: {url}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"올바르지 않은 URL입니다 (http/https 형식이어야 합니다): {url}")


def crawl_and_save_cafeteria(
    url: str,
    rest_type: RestaurantType,
    db_session: Session,
) -> CafeteriaSyncResult:
    _validate_url(url)
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
            return self._state.to_dict()

    # ------------------------------------------------------------------ #
    #  내부: 식당 하나 크롤링                                              #
    # ------------------------------------------------------------------ #

    def _crawl_one(self, url: str, rest_type: RestaurantType) -> None:
        """식당 하나를 크롤링하고 self._state.results[rest_type.value]를 업데이트합니다."""
        key = rest_type.value
        with self._lock:
            self._state.results[key].status = "running"

        logging.info("식단 크롤링 시작: restaurant_type=%s, url=%s", key, url)
        try:
            ensure_cafeteria_schema()
            with get_db_session() as db_session:
                result = crawl_and_save_cafeteria(url, rest_type, db_session)

            with self._lock:
                r = self._state.results[key]
                r.status = "completed" if result.updated else "unchanged"
                r.saved_daily_menus = len(result.menus) if result.updated else 0
                r.updated = result.updated
                r.menus = serialize_daily_menus(result.menus)

        except Exception as exc:
            logging.exception("식단 크롤링 실패: restaurant_type=%s, error=%s", key, exc)
            with self._lock:
                r = self._state.results[key]
                r.status = "failed"
                r.error = str(exc)

    # ------------------------------------------------------------------ #
    #  공개 API                                                            #
    # ------------------------------------------------------------------ #

    def run_crawlers(
        self,
        targets: list[tuple[str, RestaurantType]] | None = None,
        retry_count: int = 0,
        _preclaimed: bool = False,
    ) -> None:
        """
        targets: [(url, rest_type), ...] 리스트.
                 None 이면 CAFETERIA_URLS에 정의된 전체 식당을 대상으로 합니다.
        """
        if targets is None:
            targets = [
                (url, RestaurantType(key))
                for key, url in CAFETERIA_URLS.items()
            ]

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
                started_at=utc_now_iso(),
                retry_count=retry_count,
                results={
                    rest_type.value: RestaurantResult(url=url)
                    for url, rest_type in targets
                },
            )

        # 순차 크롤링 (학교 서버 부하 방지)
        for url, rest_type in targets:
            self._crawl_one(url, rest_type)

        with self._lock:
            statuses = {r.status for r in self._state.results.values()}
            if statuses == {"unchanged"}:
                overall = "unchanged"
            elif "failed" in statuses and statuses <= {"failed", "unchanged"}:
                overall = "failed"
            elif "failed" in statuses:
                overall = "partial_failed"
            else:
                overall = "completed"

            self._state.status = overall
            self._state.finished_at = utc_now_iso()

        any_updated = any(
            r.updated for r in self._state.results.values() if r.updated is not None
        )
        all_failed = all(r.status == "failed" for r in self._state.results.values())

        if any_updated:
            self._cancel_retry()
        elif not all_failed and retry_count < MAX_RETRY_COUNT:
            self._schedule_retry(targets, retry_count + 1)

    def start_background_run(
        self,
        targets: list[tuple[str, RestaurantType]] | None = None,
    ) -> dict:
        with self._lock:
            if self._state.status == "running" or self._background_starting:
                raise RuntimeError("이미 식단 크롤링이 실행 중입니다.")
            self._background_starting = True
            self._state.status = "starting"

        def run() -> None:
            try:
                self.run_crawlers(targets, _preclaimed=True)
            except Exception:
                logging.exception("백그라운드 식단 크롤링 실패")

        thread = threading.Thread(target=run, name="cafeteria-crawler-runner", daemon=True)
        thread.start()
        return self.get_state()

    def _cancel_retry(self) -> None:
        with self._lock:
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
            self._state.next_retry_at = None

    def _schedule_retry(
        self,
        targets: list[tuple[str, RestaurantType]],
        retry_count: int,
    ) -> None:
        next_retry = datetime.now(timezone.utc) + RETRY_DELAY

        def retry() -> None:
            try:
                self.run_crawlers(targets, retry_count)
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


cafeteria_crawl_service = CafeteriaCrawlService()