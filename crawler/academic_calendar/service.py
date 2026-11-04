import logging
import threading

import requests
from sqlalchemy.exc import SQLAlchemyError

from ..database import get_calendar_db_session
from ..http import create_session, fetch_document
from .parser import CalendarValidationError, parse_calendar, source_url
from .repository import CalendarRepository, LeaseLost, SchemaError


def safe_error(exc):
    if isinstance(exc, (CalendarValidationError, SchemaError, LeaseLost)):
        return str(exc)
    if isinstance(exc, requests.RequestException):
        return f"학교 HTTP 요청 실패: {type(exc).__name__}"
    if isinstance(exc, SQLAlchemyError):
        return f"DB 처리 실패: {type(exc).__name__}. 필수 스키마/접속/권한을 확인하세요."
    return f"실행 실패: {type(exc).__name__}"


class AcademicCalendarCrawlService:
    def __init__(self, session_factory=None, fetcher=None):
        self.repository = CalendarRepository(session_factory or get_calendar_db_session)
        self.fetcher = fetcher or self._fetch

    @staticmethod
    def _fetch(url):
        with create_session() as session:
            return fetch_document(session, url).html_text

    def start_background_run(self, year, semesters):
        run_id = self.repository.claim(year, semesters)
        try:
            threading.Thread(target=self._execute, args=(run_id, year, tuple(semesters)),
                             name=f"academic-calendar-{run_id}", daemon=True).start()
        except Exception as exc:
            self.repository.finish(run_id, safe_error(exc))
            raise
        return {"status": "starting", "run_id": run_id}

    def _execute(self, run_id, year, semesters):
        try:
            for semester in semesters:
                self.repository.begin_semester(run_id, semester)
                try:
                    url = source_url(year, semester)
                    snapshot = parse_calendar(self.fetcher(url), year, semester, url)
                    self.repository.apply(run_id, snapshot)
                except LeaseLost:
                    raise
                except Exception as exc:
                    error = safe_error(exc)
                    if self.repository.fail_semester(run_id, semester, error):
                        logging.error("학사일정 수집 실패: run_id=%s year=%s semester=%s error=%s", run_id, year, semester, error)
                    else:
                        logging.warning("DB 예외 이후 학기 완료 이력 확인: run_id=%s semester=%s. 커밋된 결과를 보존합니다.", run_id, semester)
            self.repository.finish(run_id)
        except Exception as exc:
            error = safe_error(exc)
            logging.error("학사일정 실행 중단: run_id=%s error=%s", run_id, error)
            try:
                self.repository.finish(run_id, error)
            except Exception:
                # DB outage/process crash leaves a durable starting/running run. The
                # next POST or status read recovers it after the bounded lease.
                logging.error("학사일정 종료 기록 실패: run_id=%s. lease 만료 후 상태 조회/다음 요청에서 복구합니다.", run_id)

    def get_state(self, run_id=None):
        return self.repository.get_state(run_id)


academic_calendar_crawl_service = AcademicCalendarCrawlService()
