import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session, selectinload

from ..config import CAFETERIA_URLS, REQUEST_TIMEOUT
from ..database import ensure_cafeteria_schema, get_db_session
from ..models import CafeteriaCrawlProgress, DailyMenu, RestaurantType
from .parser import parse_cafeteria_menu


KOREA_TIMEZONE = timezone(timedelta(hours=9), name="Asia/Seoul")


def korea_today() -> date:
    return datetime.now(KOREA_TIMEZONE).date()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _utc_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _completed_today(progress: CafeteriaCrawlProgress | None, business_date: date) -> bool:
    return bool(
        progress is not None
        and progress.last_success_date == business_date
        and progress.status in {"completed", "unchanged"}
    )


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
    status: str = "pending"          # pending | running | completed | unchanged | skipped | failed
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
    business_date: str | None = None
    only_pending: bool = False
    # 기존 클라이언트 호환용. 재시도는 Spring이 관리하므로 항상 0/0/null.
    retry_count: int = 0
    max_retry_count: int = 0
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
    *,
    business_date: date | None = None,
    commit: bool = True,
) -> CafeteriaSyncResult:
    _validate_url(url)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    menus = parse_cafeteria_menu(response.text, rest_type)
    if not menus:
        raise ValueError("크롤링 결과에 유효한 식단이 없습니다. 기존 데이터는 유지합니다.")

    business_date = business_date or korea_today()
    week_start = business_date - timedelta(days=business_date.weekday())
    week_end = week_start + timedelta(days=6)
    if any(not week_start <= menu.menu_date <= week_end for menu in menus):
        raise ValueError(
            f"이번 주({week_start}~{week_end}) 식단이 아닙니다. 기존 데이터는 유지합니다."
        )

    existing_menus = (
        db_session.query(DailyMenu)
        .options(selectinload(DailyMenu.meal_sections))
        .filter(
            DailyMenu.restaurant_type == rest_type,
        )
        .order_by(DailyMenu.menu_date)
        .all()
    )
    if _menu_content(existing_menus) == _menu_content(menus):
        return CafeteriaSyncResult(menus=existing_menus, updated=False)

    for existing_menu in existing_menus:
        db_session.delete(existing_menu)
    # 같은 식당/날짜 UNIQUE 제약을 지키도록 삭제를 먼저 flush한다.
    # commit 전이므로 후속 저장 실패 시 기존 식단도 함께 복구된다.
    db_session.flush()
    db_session.add_all(menus)
    db_session.flush()
    if commit:
        db_session.commit()
    return CafeteriaSyncResult(menus=menus, updated=True)


class CafeteriaCrawlService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = CafeteriaRunState()

    def get_state(self) -> dict:
        with self._lock:
            return self._state.to_dict()

    def get_daily_status(self) -> dict:
        business_date = korea_today()
        with get_db_session() as session:
            progress_by_type = {
                row.restaurant_type: row
                for row in session.query(CafeteriaCrawlProgress).all()
            }
            results = {}
            for rest_type in RestaurantType:
                row = progress_by_type.get(rest_type.value)
                results[rest_type.value] = {
                    "completed_today": _completed_today(row, business_date),
                    "status": row.status if row else "pending",
                    "business_date": row.business_date.isoformat() if row else None,
                    "run_id": row.run_id if row else None,
                    "last_attempt_at": _utc_iso(row.last_attempt_at) if row else None,
                    "last_success_date": row.last_success_date.isoformat() if row and row.last_success_date else None,
                    "last_success_at": _utc_iso(row.last_success_at) if row else None,
                    "error": row.error if row else None,
                }
        return {
            "business_date": business_date.isoformat(),
            "timezone": "Asia/Seoul",
            "pending_restaurant_types": [key for key, result in results.items() if not result["completed_today"]],
            "results": results,
        }

    def _crawl_one(self, url: str, rest_type: RestaurantType, business_date: date, only_pending: bool) -> None:
        key = rest_type.value
        with self._lock:
            self._state.results[key].status = "running"
            run_id = self._state.run_id

        logging.info("식단 크롤링 시작: restaurant_type=%s, url=%s", key, url)
        try:
            with get_db_session() as db_session:
                progress = db_session.get(CafeteriaCrawlProgress, key)
                if only_pending and _completed_today(progress, business_date):
                    with self._lock:
                        self._state.results[key].status = "skipped"
                        self._state.results[key].updated = False
                    return
                if progress is None:
                    progress = CafeteriaCrawlProgress(restaurant_type=key)
                    db_session.add(progress)
                progress.business_date = business_date
                progress.run_id = run_id
                progress.last_attempt_at = utc_now().replace(tzinfo=None)
                progress.status = "running"
                progress.error = None
                db_session.commit()

                result = crawl_and_save_cafeteria(
                    url, rest_type, db_session, business_date=business_date, commit=False
                )
                menus_json = serialize_daily_menus(result.menus)
                progress.status = "completed" if result.updated else "unchanged"
                progress.last_success_date = business_date
                progress.last_success_at = utc_now().replace(tzinfo=None)
                # 식단 반영과 성공 기록은 한 트랜잭션으로 확정한다.
                db_session.commit()

            with self._lock:
                r = self._state.results[key]
                r.status = "completed" if result.updated else "unchanged"
                r.saved_daily_menus = len(result.menus) if result.updated else 0
                r.updated = result.updated
                r.menus = menus_json

        except Exception as exc:
            logging.exception("식단 크롤링 실패: restaurant_type=%s, error=%s", key, exc)
            try:
                with get_db_session() as session:
                    progress = session.get(CafeteriaCrawlProgress, key)
                    if progress is None:
                        progress = CafeteriaCrawlProgress(restaurant_type=key)
                        session.add(progress)
                    progress.business_date = business_date
                    progress.run_id = run_id
                    progress.last_attempt_at = utc_now().replace(tzinfo=None)
                    progress.status = "failed"
                    progress.error = str(exc)
                    session.commit()
            except Exception:
                logging.exception("식단 수집 실패 기록 저장 실패: restaurant_type=%s", key)
            with self._lock:
                r = self._state.results[key]
                r.status = "failed"
                r.error = str(exc)

    def _claim_run(self, targets, only_pending: bool) -> tuple[list[tuple[str, RestaurantType]], date, dict]:
        if targets is None:
            targets = [
                (url, RestaurantType(key))
                for key, url in CAFETERIA_URLS.items()
            ]

        targets = list(dict.fromkeys(targets))
        business_date = korea_today()
        with self._lock:
            if self._state.status in {"starting", "running"}:
                raise RuntimeError("이미 식단 크롤링이 실행 중입니다.")
            self._state = CafeteriaRunState(
                run_id=uuid.uuid4().hex,
                status="starting",
                started_at=utc_now_iso(),
                business_date=business_date.isoformat(),
                only_pending=only_pending,
                results={
                    rest_type.value: RestaurantResult(url=url)
                    for url, rest_type in targets
                },
            )
            accepted = self._state.to_dict()
        return targets, business_date, accepted

    def _execute(self, targets, business_date: date, only_pending: bool) -> dict:
        with self._lock:
            self._state.status = "running"
        try:
            ensure_cafeteria_schema()
            # 순차 크롤링. 개별 실패가 다른 식당의 시도를 막지 않는다.
            for url, rest_type in targets:
                self._crawl_one(url, rest_type, business_date, only_pending)
        except Exception as exc:
            logging.exception("식단 크롤링 실행 실패")
            with self._lock:
                for result in self._state.results.values():
                    if result.status in {"pending", "running"}:
                        result.status = "failed"
                        result.error = str(exc)

        with self._lock:
            statuses = {r.status for r in self._state.results.values()}
            if statuses <= {"unchanged", "skipped"}:
                overall = "unchanged"
            elif statuses == {"failed"}:
                overall = "failed"
            elif "failed" in statuses:
                overall = "partial_failed"
            else:
                overall = "completed"

            self._state.status = overall
            self._state.finished_at = utc_now_iso()
            return self._state.to_dict()

    def run_crawlers(
        self,
        targets: list[tuple[str, RestaurantType]] | None = None,
        *,
        only_pending: bool = False,
    ) -> dict:
        targets, business_date, _ = self._claim_run(targets, only_pending)
        return self._execute(targets, business_date, only_pending)

    def start_background_run(
        self,
        targets: list[tuple[str, RestaurantType]] | None = None,
        *,
        only_pending: bool = False,
    ) -> dict:
        targets, business_date, accepted = self._claim_run(targets, only_pending)
        thread = threading.Thread(
            target=self._execute,
            args=(targets, business_date, only_pending),
            name="cafeteria-crawler-runner",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._state.status = "failed"
                self._state.finished_at = utc_now_iso()
            raise
        return accepted


cafeteria_crawl_service = CafeteriaCrawlService()
