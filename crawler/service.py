import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .models import CrawlStats
from .notices import build_crawlers, get_available_notice_types
from .runtime import configure_logging, ensure_runtime_directories


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CrawlRunState:
    run_id: str | None = None
    status: str = "idle"
    requested_notice_types: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    current_notice_type: str | None = None
    results: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str | None = None


class CrawlService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = CrawlRunState()

    def get_available_notice_types(self) -> list[str]:
        return get_available_notice_types()

    def get_state(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def run_crawlers(self, notice_types: list[str] | None = None) -> dict[str, CrawlStats]:
        configure_logging()
        ensure_runtime_directories()

        selected_types = notice_types or self.get_available_notice_types()
        run_id = uuid.uuid4().hex

        with self._lock:
            if self._state.status == "running":
                raise RuntimeError("이미 크롤링이 실행 중입니다.")

            self._state = CrawlRunState(
                run_id=run_id,
                status="running",
                requested_notice_types=selected_types,
                started_at=utc_now_iso(),
                current_notice_type=None,
                results={},
                error=None,
            )

        logging.info("크롤링 배치 시작: run_id=%s, notice_types=%s", run_id, selected_types)

        try:
            results: dict[str, CrawlStats] = {}
            for crawler in build_crawlers(selected_types):
                with self._lock:
                    self._state.current_notice_type = crawler.board.key

                logging.info(
                    "크롤러 시작: run_id=%s, notice_type=%s, name=%s",
                    run_id,
                    crawler.board.key,
                    crawler.board.name,
                )
                stats = crawler.crawl_and_sync_notices()
                results[crawler.board.key] = stats

                with self._lock:
                    self._state.results[crawler.board.key] = asdict(stats)

                logging.info(
                    "크롤러 완료: run_id=%s, notice_type=%s, pages_processed=%s, notices_inserted=%s, notices_updated=%s, notices_failed=%s, notices_deleted=%s",
                    run_id,
                    crawler.board.key,
                    stats.pages_processed,
                    stats.notices_inserted,
                    stats.notices_updated,
                    stats.notices_failed,
                    stats.notices_deleted,
                )

            with self._lock:
                self._state.status = "completed"
                self._state.finished_at = utc_now_iso()
                self._state.current_notice_type = None

            return results
        except Exception as exc:
            logging.exception("크롤링 배치 실패: run_id=%s, error=%s", run_id, exc)
            with self._lock:
                self._state.status = "failed"
                self._state.finished_at = utc_now_iso()
                self._state.current_notice_type = None
                self._state.error = str(exc)
            raise

    def start_background_run(self, notice_types: list[str] | None = None) -> dict:
        selected_types = notice_types or self.get_available_notice_types()

        with self._lock:
            if self._state.status == "running":
                raise RuntimeError("이미 크롤링이 실행 중입니다.")

        def run() -> None:
            self.run_crawlers(selected_types)

        thread = threading.Thread(target=run, name="crawler-runner", daemon=True)
        thread.start()
        return self.get_state()


crawl_service = CrawlService()
