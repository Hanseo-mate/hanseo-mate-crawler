import threading
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crawler.api import app
from crawler.cafeteria import migrate
from crawler.cafeteria.service import CafeteriaCrawlService, korea_today
from crawler.database import ensure_cafeteria_schema
from crawler.models import Base, CafeteriaCrawlProgress, DailyMenu, RestaurantType


MONDAY = date(2026, 10, 5)
STUDENT = RestaurantType.MAIN_STUDENT
TAEAN = RestaurantType.TAEAN_STUDENT
TARGETS = [("https://example.test/seosan", STUDENT), ("https://example.test/taean", TAEAN)]


def response(menu_date="2026.10.05", dish="쌀밥"):
    html = (
        f'<div class="fd_info"><p class="txt">{menu_date}</p></div>'
        '<div class="fd_table"><table><tbody>'
        f'<tr><td>월요일</td><td>{dish}</td><td></td></tr>'
        '</tbody></table></div>'
    )
    result = Mock(text=html, apparent_encoding="utf-8", encoding="utf-8")
    result.raise_for_status.return_value = None
    return result


class DailyCrawlTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.service = CafeteriaCrawlService()
        self.addCleanup(self.engine.dispose)
        self.clock = self.start_patch("crawler.cafeteria.service.korea_today", return_value=MONDAY)
        self.start_patch("crawler.cafeteria.service.get_db_session", self.sessions)
        self.start_patch("crawler.database.SQLALCHEMY_ENGINE", self.engine)
        self.http = self.start_patch("crawler.cafeteria.service.requests.get", return_value=response())

    def start_patch(self, name, *args, **kwargs):
        patcher = patch(name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_student(self, **kwargs):
        return self.service.run_crawlers(TARGETS[:1], **kwargs)

    def dishes(self):
        with self.sessions() as session:
            return [section.dishes for menu in session.query(DailyMenu).all() for section in menu.meal_sections]

    def test_success_survives_service_restart_and_next_day_fetches_changed_menu(self):
        first = self.run_student(only_pending=True)
        self.assertEqual(first["results"][STUDENT.value]["status"], "completed")
        self.service = CafeteriaCrawlService()
        second = self.run_student(only_pending=True)
        self.assertEqual(second["results"][STUDENT.value]["status"], "skipped")
        self.assertEqual(self.http.call_count, 1)
        self.clock.return_value = date(2026, 10, 6)
        self.http.return_value = response(dish="수정한 메뉴")
        self.assertFalse(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])
        self.run_student(only_pending=True)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.dishes(), [["수정한 메뉴"]])

    def test_unchanged_current_week_is_success_for_new_day(self):
        self.run_student()
        self.clock.return_value = date(2026, 10, 6)
        result = self.run_student(only_pending=True)
        self.assertEqual(result["results"][STUDENT.value]["status"], "unchanged")
        self.assertTrue(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])
        self.run_student(only_pending=True)
        self.assertEqual(self.http.call_count, 2)

    def test_old_future_or_empty_source_preserves_saved_menus_and_remains_pending(self):
        self.run_student()
        self.clock.return_value = date(2026, 10, 6)
        for bad_response in [response("2026.09.28"), response("2026.10.12"), response(dish="")]:
            with self.subTest(source=bad_response.text):
                self.http.return_value = bad_response
                with self.assertLogs(level="ERROR"):
                    result = self.run_student(only_pending=True)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])
                self.assertEqual(self.dishes(), [["쌀밥"]])

    def test_partial_failure_retries_only_failed_restaurant(self):
        self.http.side_effect = [response(), requests.Timeout("school unavailable")]
        with self.assertLogs(level="ERROR"):
            result = self.service.run_crawlers(TARGETS, only_pending=True)
        self.assertEqual(result["status"], "partial_failed")
        self.http.side_effect = None
        self.http.reset_mock()
        self.service.run_crawlers(TARGETS, only_pending=True)
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args[0], TARGETS[1][0])

    def test_all_failures_stay_pending_without_internal_timer_or_attempt_limit(self):
        self.http.side_effect = requests.Timeout("school unavailable")
        with patch("threading.Timer") as timer, self.assertLogs(level="ERROR"):
            for _ in range(9):
                result = self.run_student(only_pending=True)
                self.assertEqual(result["status"], "failed")
            timer.assert_not_called()
        self.assertEqual(self.http.call_count, 9)
        self.assertIsNone(result["next_retry_at"])
        self.assertEqual(result["max_retry_count"], 0)

    def test_failed_success_record_rolls_back_menu_replacement(self):
        self.run_student()
        self.clock.return_value = date(2026, 10, 6)
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TRIGGER reject_success BEFORE UPDATE ON cafeteria_crawl_progress
                WHEN NEW.status = 'completed'
                BEGIN SELECT RAISE(ABORT, 'progress write failed'); END
            """))
        self.http.return_value = response(dish="저장되면 안 되는 메뉴")
        with self.assertLogs(level="ERROR"):
            result = self.run_student(only_pending=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.dishes(), [["쌀밥"]])
        self.assertFalse(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])

    def test_abandoned_running_record_is_retryable_after_restart(self):
        with self.sessions() as session:
            session.add(CafeteriaCrawlProgress(
                restaurant_type=STUDENT.value, business_date=MONDAY, status="running",
                run_id="interrupted", last_attempt_at=datetime(2026, 10, 4, 16),
            ))
            session.commit()
        self.run_student(only_pending=True)
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])

    def test_api_sync_and_persisted_daily_status(self):
        with patch("crawler.api.cafeteria_crawl_service", self.service), TestClient(app) as client:
            payload = {"mode": "sync", "only_pending": True, "restaurant_types": [STUDENT.value]}
            self.assertEqual(client.post("/cafeteria-crawl/run", json=payload).status_code, 200)
            second = client.post("/cafeteria-crawl/run", json=payload).json()
            self.assertEqual(second["results"][STUDENT.value]["status"], "skipped")
            self.assertEqual(self.http.call_count, 1)
            self.http.return_value = response(dish="수동 수정")
            client.post("/cafeteria-crawl/run", json={"mode": "sync", "restaurant_types": [STUDENT.value]})
            self.assertEqual(self.dishes(), [["수동 수정"]])
            daily = client.get("/cafeteria-crawl/daily-status").json()
            self.assertTrue(daily["results"][STUDENT.value]["completed_today"])
            self.assertEqual(daily["business_date"], "2026-10-05")

    def test_background_acceptance_is_not_success_and_concurrent_request_is_409(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original = self.service._execute

        def blocking(*args):
            entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError("test did not release background worker")
                return original(*args)
            finally:
                finished.set()

        with patch.object(self.service, "_execute", side_effect=blocking), patch("crawler.api.cafeteria_crawl_service", self.service), TestClient(app) as client:
            try:
                result = client.post("/cafeteria-crawl/run", json={"only_pending": True, "restaurant_types": [STUDENT.value]}).json()
                self.assertEqual(result["status"], "starting")
                self.assertTrue(entered.wait(2))
                self.assertFalse(client.get("/cafeteria-crawl/daily-status").json()["results"][STUDENT.value]["completed_today"])
                self.assertEqual(client.post("/cafeteria-crawl/run", json={}).status_code, 409)
            finally:
                release.set()
                self.assertTrue(finished.wait(5))
        self.assertTrue(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])

    def test_run_crossing_midnight_does_not_count_for_next_day(self):
        def crossing_midnight(*args, **kwargs):
            self.clock.return_value = date(2026, 10, 6)
            return response()
        self.http.side_effect = crossing_midnight
        self.run_student(only_pending=True)
        self.assertFalse(self.service.get_daily_status()["results"][STUDENT.value]["completed_today"])
        self.run_student(only_pending=True)
        self.assertEqual(self.http.call_count, 2)

    def test_migration_is_idempotent_and_preserves_food(self):
        self.run_student()
        with patch("crawler.cafeteria.migrate.get_sqlalchemy_engine", return_value=self.engine):
            migrate.main()
            migrate.main()
        self.assertEqual(self.dishes(), [["쌀밥"]])


class SchemaAndClockTest(unittest.TestCase):
    def test_korea_date_is_used_at_one_am(self):
        utc_time = datetime(2026, 10, 4, 16, tzinfo=timezone.utc)
        with patch("crawler.cafeteria.service.datetime") as clock:
            clock.now.side_effect = lambda tz: utc_time.astimezone(tz)
            self.assertEqual(korea_today(), MONDAY)

    def test_incompatible_old_schema_is_not_dropped(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE meal_sections (id INTEGER, menu_category TEXT)"))
            connection.execute(text("CREATE TABLE dishes (name TEXT)"))
            connection.execute(text("INSERT INTO dishes VALUES ('keep')"))
        with patch("crawler.database.SQLALCHEMY_ENGINE", engine), self.assertRaisesRegex(RuntimeError, "마이그레이션"):
            ensure_cafeteria_schema()
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT name FROM dishes")).scalar(), "keep")
        self.assertIn("meal_sections", inspect(engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
