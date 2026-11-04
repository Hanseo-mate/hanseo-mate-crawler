"""Opt-in MySQL 8 test; never reads crawler DB_CONFIG for a connection.

ACADEMIC_CALENDAR_TEST_MYSQL_URL must identify an EMPTY localhost database named
hanseo_calendar_test_<suffix>. Only this disposable database is mutated.
"""
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from crawler.academic_calendar.migrate import migrate
from crawler.academic_calendar.models import CalendarCrawlLock, CalendarCrawlRun, SchoolCalendarEvent
from crawler.academic_calendar.parser import parse_calendar, source_url
from crawler.academic_calendar.repository import AlreadyRunning, CalendarRepository, check_schema

TEST_URL = os.getenv('ACADEMIC_CALENDAR_TEST_MYSQL_URL')
FIXTURES = Path(__file__).parent / 'fixtures' / 'academic_calendar'


@unittest.skipUnless(TEST_URL, 'Requires an isolated MySQL 8 test database; no operational DB fallback')
class MySQLCalendarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if (url.drivername != 'mysql+pymysql' or url.host not in ('127.0.0.1', 'localhost')
                or not re.fullmatch(r'hanseo_calendar_test_[a-z0-9_]+', url.database or '')):
            raise RuntimeError('Only localhost mysql+pymysql / hanseo_calendar_test_<suffix> is permitted')
        cls.engine = create_engine(url, connect_args={'connect_timeout': 3, 'read_timeout': 5, 'write_timeout': 5})
        cls.addClassCleanup(cls.engine.dispose)
        if inspect(cls.engine).get_table_names():
            raise RuntimeError('Test database must be empty; existing tables will not be modified')
        with cls.engine.begin() as conn:
            # Pre-migration Spring table; new fields come from the exact Spring SQL fixture.
            conn.execute(text('''CREATE TABLE school_calendar_events (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                start_date DATE NOT NULL, end_date DATE NOT NULL, title VARCHAR(500) NOT NULL,
                created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''))
            for statement in (FIXTURES / '0005_academic_calendar.sql').read_text(encoding='utf8').split(';'):
                if statement.strip():
                    conn.exec_driver_sql(statement)
        migrate(cls.engine)
        cls.sessions = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        # Validated empty disposable database above; no production configuration used.
        with cls.engine.begin() as conn:
            for table in (CalendarCrawlRun.__table__, CalendarCrawlLock.__table__, SchoolCalendarEvent.__table__):
                table.drop(conn)
            conn.execute(text('DROP TABLE academic_holiday_months'))

    def setUp(self):
        self.repo = CalendarRepository(self.sessions)
        with self.engine.begin() as conn:
            conn.execute(CalendarCrawlRun.__table__.delete())
            conn.execute(SchoolCalendarEvent.__table__.delete())
            conn.execute(CalendarCrawlLock.__table__.update().values(owner_run_id=None, expires_at=None))
        self.snapshot = parse_calendar((FIXTURES / '2026_1.html').read_text(encoding='utf8'), 2026, 1, source_url(2026, 1))

    def test_spring_schema_bit_dates_and_idempotent_update(self):
        with self.engine.connect() as conn:
            check_schema(conn, [SchoolCalendarEvent.__table__, CalendarCrawlRun.__table__, CalendarCrawlLock.__table__])
        first = self.repo.claim(2026, [1])
        self.repo.begin_semester(first, 1)
        self.repo.apply(first, self.snapshot)
        self.repo.finish(first)
        with self.sessions() as session:
            before = [(r.id, r.created_at, r.start_date, r.active) for r in session.scalars(select(SchoolCalendarEvent).order_by(SchoolCalendarEvent.id))]
        second = self.repo.claim(2026, [1])
        self.repo.begin_semester(second, 1)
        self.repo.apply(second, self.snapshot)
        self.repo.finish(second)
        self.assertEqual(self.repo.get_state(second)['results']['1']['unchanged'], 34)
        with self.sessions() as session:
            after = [(r.id, r.created_at, r.start_date, r.active) for r in session.scalars(select(SchoolCalendarEvent).order_by(SchoolCalendarEvent.id))]
        self.assertEqual(before, after)
        self.assertTrue(all(r[3] for r in after))

    def test_two_connections_claim_only_one_run(self):
        gate = threading.Barrier(2)
        results = []
        def claim():
            gate.wait(5)
            try:
                results.append(CalendarRepository(self.sessions).claim(2026, [1]))
            except AlreadyRunning:
                results.append('busy')
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 2)
        self.assertEqual(results.count('busy'), 1)

    def test_result_failure_rolls_back_inserted_events(self):
        run_id = self.repo.claim(2026, [1])
        self.repo.begin_semester(run_id, 1)
        with patch.object(self.repo, 'record_result', side_effect=RuntimeError('history write failed')):
            with self.assertRaises(RuntimeError):
                self.repo.apply(run_id, self.snapshot)
        with self.sessions() as session:
            self.assertIsNone(session.scalars(select(SchoolCalendarEvent)).first())
        self.repo.fail_semester(run_id, 1, 'test rollback')
        self.repo.finish(run_id)
        self.assertEqual(self.repo.get_state(run_id)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
