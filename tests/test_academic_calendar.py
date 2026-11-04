from datetime import date, datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import requests
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects import mysql

from crawler.api import app
from crawler.academic_calendar.migrate import migrate
from crawler.academic_calendar.models import CalendarBase, CalendarCrawlLock, CalendarCrawlRun, SchoolCalendarEvent
from crawler.academic_calendar.parser import CalendarValidationError, classify, parse_calendar, parse_dates, source_url
from crawler.academic_calendar.repository import AlreadyRunning, CalendarRepository, LeaseLost, SchemaError, check_schema
from crawler.academic_calendar.service import AcademicCalendarCrawlService

FIXTURES = Path(__file__).parent / 'fixtures' / 'academic_calendar'


def html(semester=1):
    return (FIXTURES / f'2026_{semester}.html').read_text(encoding='utf-8')


def mutate(action, semester=1):
    soup = BeautifulSoup(html(semester), 'html5lib')
    action(soup)
    return str(soup)


def remove_events(soup, event_id=None):
    selector = f'li[id="{event_id}"]' if event_id else 'ul.list_blue > li'
    for item in soup.select(selector):
        item.decompose()


class ParserTest(unittest.TestCase):
    def test_real_html_all_events_and_core_dates(self):
        for semester, count in ((1, 34), (2, 33)):
            result = parse_calendar(html(semester), 2026, semester, source_url(2026, semester))
            self.assertEqual(len(result.events), count)
            self.assertEqual(len({e.source_event_id for e in result.events}), count)
            types = {e.event_type: e for e in result.events if e.event_type != 'OTHER'}
            self.assertEqual(len(types), 4)
            self.assertEqual(types['MIDTERM'].start_date, date(2026, 4 if semester == 1 else 10, 20))
            self.assertEqual(types['VACATION_START'].start_date, date(2026, 6 if semester == 1 else 12, 22))

    def test_date_shapes(self):
        cases = [
            ('02일', 1, 3, '2026-03-02', '2026-03-02'),
            ('20일~24일', 1, 4, '2026-04-20', '2026-04-24'),
            ('28일~10월02일', 2, 9, '2026-09-28', '2026-10-02'),
            ('22일~01월13일', 2, 12, '2026-12-22', '2027-01-13'),
            ('04일~06일', 2, 1, '2027-01-04', '2027-01-06'),
            ('02일~04일', 2, 2, '2027-02-02', '2027-02-04'),
        ]
        for value, semester, month, start, end in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_dates(value, 2026, semester, month), (date.fromisoformat(start), date.fromisoformat(end)))

    def test_invalid_and_ambiguous_dates_fail(self):
        for value in ['31일', '20일~19일', '20~24', '미정', '03월20일', '20일~13월01일']:
            with self.subTest(value=value), self.assertRaises(CalendarValidationError):
                parse_dates(value, 2026, 1, 4)

    def test_regular_event_can_end_in_the_next_semester(self):
        cases = [(1, 8, '31일~09월04일', date(2026, 8, 31), date(2026, 9, 4)),
                 (2, 2, '26일~03월05일', date(2027, 2, 26), date(2027, 3, 5))]
        for semester, month, value, start, end in cases:
            with self.subTest(semester=semester):
                self.assertEqual(parse_dates(value, 2026, semester, month), (start, end))

    def test_core_chronology_and_page_identity_conflicts_fail(self):
        replacements = [
            ('1학기 중간고사', 'TEMP', '1학기 기말고사', '1학기 중간고사'),
            ('22일&nbsp;:&nbsp;하계방학', '18일&nbsp;:&nbsp;하계방학'),
            ('1학기 중간고사', '2학기 중간고사'),
            ('1학기 중간고사', '2027학년도 1학기 중간고사'),
        ]
        for changes in replacements:
            page = html()
            if len(changes) == 4:
                page = page.replace(changes[0], changes[1]).replace(changes[2], changes[3]).replace('TEMP', '1학기 기말고사')
            else:
                page = page.replace(*changes)
            with self.subTest(changes=changes), self.assertRaises(CalendarValidationError):
                parse_calendar(page, 2026, 1, source_url(2026, 1))

    def test_classification_does_not_invent_key_events(self):
        for title in ['중간 강의평가', '학기개시일 30일', '학기개시일 60일', '학기개시일 90일',
                      '하계 계절학기', '동계 계절학기 수강신청', '중간고사 성적열람', '하계방학 특강']:
            self.assertEqual(classify(title), 'OTHER', title)
        for title, expected in [('2026학년도 1학기 학기개시일', 'SEMESTER_START'),
                                ('학기개시일', 'SEMESTER_START'),
                                ('1학기 중간고사', 'MIDTERM'), ('2학기 기말고사', 'FINAL'),
                                ('하계방학', 'VACATION_START'), ('동계 방학 시작일', 'VACATION_START')]:
            self.assertEqual(classify(title), expected)

    def test_next_semester_title_does_not_change_page_metadata(self):
        result = parse_calendar(html(2), 2026, 2, source_url(2026, 2))
        event = next(e for e in result.events if e.source_event_id == 'date1117172')
        self.assertEqual(event.title, '1학기 수강신청')
        self.assertEqual(event.start_date, date(2027, 2, 2))
        self.assertEqual((result.academic_year, result.semester), (2026, 2))

    def test_whitespace_entities_and_newlines(self):
        result = parse_calendar(html(1), 2026, 1, source_url(2026, 1))
        event = next(e for e in result.events if e.source_event_id == 'date1117131')
        self.assertEqual(event.title, '군복무 중 취득학점 신청 K-MOOC 취득학점 신청')

    def test_selected_year_and_semester_must_match(self):
        for year, semester in [(2027, 1), (2026, 2)]:
            with self.assertRaises(CalendarValidationError):
                parse_calendar(html(), year, semester, source_url(year, semester))
        page = mutate(lambda s: s.select_one('option[selected]').attrs.pop('selected'))
        with self.assertRaises(CalendarValidationError):
            parse_calendar(page, 2026, 1, source_url(2026, 1))

    def test_2027_input_is_not_hardcoded(self):
        result = parse_calendar(html().replace('2026', '2027'), 2027, 1, source_url(2027, 1))
        self.assertTrue(all(e.start_date.year == 2027 for e in result.events))

    def test_empty_month_present_differs_from_missing_month(self):
        def empty_month(soup):
            for table in soup.select('table.table_style02, table.table_style03'):
                for row in table.select('tr'):
                    cells = row.find_all('td', recursive=False)
                    if cells and cells[0].get_text(strip=True) == '7월':
                        cells[-1].clear()
        self.assertEqual(len(parse_calendar(mutate(empty_month), 2026, 1, source_url(2026, 1)).events), 30)
        def missing_month(soup):
            soup.select_one('li#date1117136').find_parent('tr').decompose()
        with self.assertRaises(CalendarValidationError):
            parse_calendar(mutate(missing_month), 2026, 1, source_url(2026, 1))

    def test_all_empty_is_unpublished_but_blank_html_is_invalid(self):
        self.assertEqual(parse_calendar(mutate(remove_events), 2026, 1, source_url(2026, 1)).events, ())
        with self.assertRaises(CalendarValidationError):
            parse_calendar('', 2026, 1, source_url(2026, 1))

    def test_bad_event_or_missing_view_never_silently_dropped(self):
        mutations = [
            lambda s: s.select_one('li[id^=date]').attrs.pop('id'),
            lambda s: setattr(s.select_one('li[id^=date]'), 'string', '날짜 없음 : 제목'),
            lambda s: setattr(s.select_one('li[id^=date]'), 'string', '02일 : ' + '가' * 501),
            lambda s: s.select_one('li[id^=date]').attrs.update(id='date' + '1' * 100),
            lambda s: s.select_one('table.table_style03').decompose(),
            lambda s: s.select_one('li[id^=date]').decompose(),
            lambda s: setattr(s.select_one('li[id^=date]'), 'string', '02일 : 서로 다른 제목'),
            lambda s: s.select_one('ul.list_blue').append('해석되지 않은 내용'),
        ]
        for action in mutations:
            with self.subTest(action=action), self.assertRaises(CalendarValidationError):
                parse_calendar(mutate(action), 2026, 1, source_url(2026, 1))

    def test_core_duplicate_ids_and_conflicting_periods_fail(self):
        for title in ['1학기 중간고사', '1학기 기말고사', '하계방학']:
            def add_core(soup):
                for ul in soup.select('table.table_style02 ul.list_blue, table.table_style03 ul.list_blue')[::6]:
                    item = soup.new_tag('li', id='date999999')
                    item.string = '03일 : ' + title
                    ul.append(item)
            with self.subTest(title=title), self.assertRaises(CalendarValidationError):
                parse_calendar(mutate(add_core), 2026, 1, source_url(2026, 1))


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.calendar-test-', dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine('sqlite:///' + str(Path(self.tmp.name) / 'calendar.sqlite'),
                                    connect_args={'check_same_thread': False})
        CalendarBase.metadata.create_all(self.engine)
        migrate(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.fetches = []
        self.pages = {1: html(1), 2: html(2)}
        self.service = AcademicCalendarCrawlService(self.sessions, self.fetch)
        self.repo = self.service.repository
        self.addCleanup(self.engine.dispose)

    def fetch(self, url):
        self.fetches.append(url)
        value = self.pages[int(url[-1])]
        if isinstance(value, Exception):
            raise value
        return value

    def run_calendar(self, semesters=(1,), year=2026):
        run_id = self.repo.claim(year, semesters)
        self.service._execute(run_id, year, semesters)
        return self.repo.get_state(run_id)

    def rows(self):
        with self.sessions() as session:
            return {r.id: {c.name: getattr(r, c.name) for c in SchoolCalendarEvent.__table__.columns}
                    for r in session.scalars(select(SchoolCalendarEvent))}

    def test_idempotent_upsert_preserves_identity_and_reactivates(self):
        self.assertEqual(self.run_calendar()['results']['1']['inserted'], 34)
        before = self.rows()
        result = self.run_calendar()['results']['1']
        self.assertEqual((result['inserted'], result['unchanged']), (0, 34))
        self.pages[1] = html().replace('수강신청 정정', '수강신청 정정 안내')
        with self.sessions.begin() as session:
            session.execute(update(SchoolCalendarEvent).where(SchoolCalendarEvent.source_event_id == 'date1117112').values(active=False))
        result = self.run_calendar()['results']['1']
        self.assertEqual(result['updated'], 1)
        after = self.rows()
        self.assertEqual(before.keys(), after.keys())
        for key in before:
            self.assertEqual(before[key]['created_at'], after[key]['created_at'])
            self.assertTrue(after[key]['active'])
            self.assertGreaterEqual(after[key]['last_synced_at'], before[key]['last_synced_at'])

    def test_deactivation_is_limited_to_requested_hanseo_scope(self):
        self.run_calendar((1, 2))
        with self.sessions.begin() as session:
            for source, year, semester in [('MANUAL', None, None), ('HANSEO', 2027, 1)]:
                session.add(SchoolCalendarEvent(source=source, academic_year=year, semester=semester,
                    source_event_id='date999', start_date=date(2026, 3, 1), end_date=date(2026, 3, 1),
                    title='보존', created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
                    event_type='OTHER', active=True))
        before = self.rows()
        self.pages[1] = mutate(lambda s: remove_events(s, 'date1117112'))
        self.assertEqual(self.run_calendar()['results']['1']['deactivated'], 1)
        after = self.rows()
        self.assertEqual(len(after), len(before))
        self.assertEqual(sum(not row['active'] for row in after.values()), 1)
        for key, row in before.items():
            if row['source'] != 'HANSEO' or row['academic_year'] != 2026 or row['semester'] != 1:
                self.assertEqual(after[key], row)

    def test_core_disappearance_defers_ambiguous_deletion(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = mutate(lambda s: remove_events(s, 'date1117118'))
        result = self.run_calendar()['results']['1']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['deactivated'], 0)
        self.assertIn('핵심 일정 종류 누락', result['error'])
        self.assertEqual(before, self.rows())

    def test_missing_core_and_replaced_exam_id_cannot_create_active_duplicates(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = mutate(lambda s: remove_events(s, 'date1117118')).replace('date1117132', 'date999999')
        result = self.run_calendar()['results']['1']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(before, self.rows())

    def test_core_reclassification_does_not_erase_last_good_exam(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = html().replace('1학기 중간고사', '중간 강의평가')
        self.assertEqual(self.run_calendar()['status'], 'failed')
        self.assertEqual(before, self.rows())

    def test_commit_ack_loss_keeps_durably_completed_semester(self):
        original = self.repo.apply
        def committed_then_disconnected(*args):
            original(*args)
            raise OperationalError('COMMIT', {}, OSError('connection lost after commit'))
        with patch.object(self.repo, 'apply', side_effect=committed_then_disconnected):
            result = self.run_calendar((1, 2))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['results']['1']['status'], 'completed')
        self.assertEqual(result['results']['1']['inserted'], 34)
        self.assertEqual(result['results']['2']['inserted'], 33)
        self.assertIsNone(result['results']['1']['error'])
        self.assertEqual(len(self.rows()), 67)

    def test_cross_semester_registration_keeps_page_metadata_in_database(self):
        for semester, event_id, title in [(1, 'date1117143', '2학기 등록'), (2, 'date1117175', '1학기 등록')]:
            def extend(soup):
                for item in soup.select(f'li[id="{event_id}"]'):
                    item.string = ('31일~09월04일' if semester == 1 else '26일~03월05일') + ' : ' + title
            self.pages[semester] = mutate(extend, semester)
        result = self.run_calendar((1, 2))
        self.assertEqual(result['status'], 'completed')
        for semester, event_id, end in [(1, 'date1117143', date(2026, 9, 4)), (2, 'date1117175', date(2027, 3, 5))]:
            row = next(r for r in self.rows().values() if r['source_event_id'] == event_id)
            self.assertEqual((row['academic_year'], row['semester'], row['end_date']), (2026, semester, end))

    def test_conflicting_core_preserves_semester_and_other_semester_still_succeeds(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = html().replace('22일&nbsp;:&nbsp;하계방학', '18일&nbsp;:&nbsp;하계방학')
        result = self.run_calendar((1, 2))
        self.assertEqual(result['status'], 'partial_failed')
        self.assertIn('방학', result['results']['1']['error'])
        self.assertEqual(result['results']['2']['status'], 'completed')
        after = self.rows()
        self.assertEqual(before, {key: row for key, row in after.items() if row['semester'] == 1})

    def test_commit_ack_loss_keeps_unpublished_record(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = mutate(remove_events)
        original = self.repo.apply
        def committed_then_disconnected(*args):
            original(*args)
            raise OperationalError('COMMIT', {}, OSError('connection lost after commit'))
        with patch.object(self.repo, 'apply', side_effect=committed_then_disconnected):
            result = self.run_calendar()
        self.assertEqual(result['status'], 'unpublished')
        self.assertEqual(before, self.rows())

    def test_new_exam_id_in_complete_snapshot_retires_only_old_exam_id(self):
        self.run_calendar()
        self.pages[1] = html().replace('date1117132', 'date999999')
        result = self.run_calendar()['results']['1']
        self.assertEqual((result['inserted'], result['deactivated']), (1, 1))
        active_finals = [r for r in self.rows().values() if r['active'] and r['event_type'] == 'FINAL']
        self.assertEqual([r['source_event_id'] for r in active_finals], ['date999999'])

    def test_removed_event_reappears_with_original_id_and_new_dates(self):
        self.run_calendar()
        before = next(row for row in self.rows().values() if row['source_event_id'] == 'date1117112')
        self.pages[1] = mutate(lambda s: remove_events(s, 'date1117112'))
        self.assertEqual(self.run_calendar()['results']['1']['deactivated'], 1)
        self.pages[1] = html().replace('05일~06일&nbsp;:&nbsp;수강신청 정정', '06일~07일&nbsp;:&nbsp;수강신청 정정')
        self.assertEqual(self.run_calendar()['results']['1']['updated'], 1)
        after = self.rows()[before['id']]
        self.assertEqual(after['created_at'], before['created_at'])
        self.assertEqual(after['start_date'], date(2026, 3, 6))
        self.assertEqual(after['end_date'], date(2026, 3, 7))
        self.assertTrue(after['active'])

    def test_failure_and_unpublished_preserve_all_saved_data(self):
        self.run_calendar()
        before = self.rows()
        for page, expected in [(mutate(remove_events), 'unpublished'), ('', 'failed'),
                               (html(2), 'failed'), (requests.Timeout('network'), 'failed'),
                               (requests.HTTPError('503'), 'failed'),
                               (mutate(lambda s: s.select_one('li[id^=date]').find_parent('tr').decompose()), 'failed')]:
            self.pages[1] = page
            with self.subTest(expected=expected):
                result = self.run_calendar()
                self.assertEqual(result['results']['1']['status'], expected)
                self.assertEqual(before, self.rows())

    def test_each_semester_result_and_requested_scope_persist_after_restart(self):
        self.pages[1] = requests.Timeout()
        result = self.run_calendar((1, 2))
        self.assertEqual(result['status'], 'partial_failed')
        self.assertEqual(result['results']['1']['status'], 'failed')
        self.assertEqual(result['results']['2']['status'], 'completed')
        restarted = AcademicCalendarCrawlService(self.sessions, self.fetch)
        self.assertEqual(restarted.get_state(result['run_id']), result)
        self.assertEqual(self.fetches, [source_url(2026, 1), source_url(2026, 2)])
        self.fetches.clear()
        self.pages[1] = html().replace('2026', '2027')
        self.assertEqual(self.run_calendar((1,), 2027)['status'], 'completed')
        self.assertEqual(self.fetches, [source_url(2027, 1)])

    def test_unpublished_aggregate_is_distinct_from_success(self):
        self.pages[1] = mutate(remove_events)
        self.assertEqual(self.run_calendar()['status'], 'unpublished')
        self.assertEqual(self.run_calendar((1, 2))['status'], 'partial_unpublished')

    def test_result_write_failure_rolls_back_entire_semester(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = html().replace('수강신청 정정', '수정')
        original = self.repo.record_result
        def fail_success(*args, **kwargs):
            if kwargs.get('status') == 'completed':
                raise RuntimeError('simulated history write failure')
            return original(*args, **kwargs)
        with patch.object(self.repo, 'record_result', side_effect=fail_success):
            result = self.run_calendar()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(before, self.rows())
        self.assertEqual(result['results']['1']['updated'], 0)

    def test_database_error_rolls_back_changes_before_failed_insert(self):
        self.run_calendar()
        before = self.rows()
        self.pages[1] = html().replace('수강신청 정정', '수정').replace('date1117143', 'date999999')
        with self.engine.begin() as connection:
            connection.execute(text("""CREATE TRIGGER reject_insert BEFORE INSERT ON school_calendar_events
                BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""))
        self.assertEqual(self.run_calendar()['status'], 'failed')
        self.assertEqual(before, self.rows())

    def test_missing_school_schema_is_explicit_failure_and_never_created(self):
        with self.engine.begin() as connection:
            connection.execute(text('DROP TABLE school_calendar_events'))
        result = self.run_calendar()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('필수 테이블 없음', result['results']['1']['error'])
        self.assertNotIn('school_calendar_events', inspect(self.engine).get_table_names())

    def test_expired_owner_is_fenced_and_status_recovers_without_new_crawl(self):
        run_id = self.repo.claim(2026, (1, 2))
        self.repo.begin_semester(run_id, 1)
        with self.sessions.begin() as session:
            session.execute(update(CalendarCrawlLock).values(expires_at=datetime(2000, 1, 1)))
        result = self.repo.get_state(run_id)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(all(r['status'] == 'failed' for r in result['results'].values()))
        snapshot = parse_calendar(html(), 2026, 1, source_url(2026, 1))
        with self.assertRaises(LeaseLost):
            self.repo.apply(run_id, snapshot)
        self.assertEqual(self.rows(), {})
        new_run = self.repo.claim(2026, (1,))
        self.assertNotEqual(new_run, run_id)
        with self.assertRaises(LeaseLost):
            self.repo.finish(run_id)

    def test_stale_run_preserves_completed_semester_and_recovers_on_claim(self):
        run_id = self.repo.claim(2026, (1, 2))
        self.repo.begin_semester(run_id, 1)
        self.repo.apply(run_id, parse_calendar(html(), 2026, 1, source_url(2026, 1)))
        with self.sessions.begin() as session:
            session.execute(update(CalendarCrawlLock).values(expires_at=datetime(2000, 1, 1)))
        self.repo.claim(2026, (2,))
        result = self.repo.get_state(run_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['results']['1']['status'], 'completed')
        self.assertEqual(result['results']['2']['status'], 'failed')
        self.assertEqual(len(self.rows()), 34)

    def test_independent_repository_instances_cannot_claim_concurrently(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def claim():
            barrier.wait(3)
            try:
                CalendarRepository(self.sessions).claim(2026, (1,))
                outcomes.append('accepted')
            except AlreadyRunning:
                outcomes.append('busy')
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertCountEqual(outcomes, ['accepted', 'busy'])

    def test_http_fast_acceptance_run_id_and_cross_instance_conflict(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked(url):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timeout')
            return self.fetch(url)
        self.service.fetcher = blocked
        execute = self.service._execute
        def tracked(*args):
            try:
                execute(*args)
            finally:
                finished.set()
        payload = dict(academic_year=2026, semesters=[2], mode='background')
        with patch.object(self.service, '_execute', side_effect=tracked), patch('crawler.api.academic_calendar_crawl_service', self.service), TestClient(app) as client:
            try:
                start = time.monotonic()
                response = client.post('/academic-calendar-crawl/run', json=payload)
                self.assertLess(time.monotonic() - start, 2)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()['status'], 'starting')
                run_id = response.json()['run_id']
                self.assertTrue(run_id)
                self.assertTrue(entered.wait(2))
                state = client.get('/academic-calendar-crawl/status', params={'run_id': run_id}).json()
                self.assertEqual(state['results']['2']['status'], 'running')
                self.assertEqual(state['results']['2']['inserted'], 0)
                another = AcademicCalendarCrawlService(self.sessions, self.fetch)
                with patch('crawler.api.academic_calendar_crawl_service', another):
                    self.assertEqual(client.post('/academic-calendar-crawl/run', json=payload).status_code, 409)
                self.assertEqual(client.get('/academic-calendar-crawl/status?run_id=missing').status_code, 404)
            finally:
                release.set()
                self.assertTrue(finished.wait(5))
        self.assertEqual(self.repo.get_state(run_id)['status'], 'completed')
        self.assertEqual(self.fetches, [source_url(2026, 2)])

    def test_http_validation_never_starts_work(self):
        valid = dict(academic_year=2026, semesters=[1, 2], mode='background')
        invalid = [{'academic_year': y} for y in (2025, 9999, '2026', True, 2026.0, None)]
        invalid += [{'semesters': s} for s in ([], [1, 1], [0], [3], [True], ['1'], [1, 2, 1], None)]
        invalid += [{'mode': 'sync'}, {'mode': 'unknown'}, {'unexpected': 1}]
        with patch('crawler.api.academic_calendar_crawl_service', self.service), patch.object(self.service, 'start_background_run') as start, TestClient(app) as client:
            for changes in invalid:
                with self.subTest(changes=changes):
                    self.assertEqual(client.post('/academic-calendar-crawl/run', json={**valid, **changes}).status_code, 422)
            self.assertEqual(client.post('/academic-calendar-crawl/run', json={}).status_code, 422)
            start.assert_not_called()

    def test_thread_start_failure_releases_lock_and_records_failure(self):
        with patch('crawler.academic_calendar.service.threading.Thread.start', side_effect=RuntimeError('thread failure')):
            with self.assertRaises(RuntimeError):
                self.service.start_background_run(2026, [1])
        self.assertEqual(self.repo.get_state()['status'], 'failed')
        self.repo.claim(2026, [1])

    def test_history_migration_idempotent_without_event_ddl(self):
        self.run_calendar()
        before = self.rows()
        migrate(self.engine)
        migrate(self.engine)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.repo.get_state()['status'], 'completed')

    def test_missing_history_returns_503_without_starting_http_fetch(self):
        with self.engine.begin() as conn:
            CalendarCrawlRun.__table__.drop(conn)
        with patch('crawler.api.academic_calendar_crawl_service', self.service), TestClient(app) as client:
            response = client.post('/academic-calendar-crawl/run', json={'academic_year': 2026, 'semesters': [1]})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(client.get('/academic-calendar-crawl/status').status_code, 503)
        self.assertEqual(self.fetches, [])
        with self.sessions() as session:
            self.assertIsNone(session.get(CalendarCrawlLock, 1).owner_run_id)

    def test_history_migration_does_not_create_missing_school_table(self):
        with self.engine.begin() as conn:
            SchoolCalendarEvent.__table__.drop(conn)
        migrate(self.engine)
        self.assertNotIn('school_calendar_events', inspect(self.engine).get_table_names())

    def test_missing_lock_row_is_unavailable_instead_of_idle(self):
        with self.engine.begin() as conn:
            conn.execute(CalendarCrawlLock.__table__.delete())
        with self.assertRaisesRegex(SchemaError, '잠금 행 없음'):
            self.repo.get_state()


class SchemaContractTest(unittest.TestCase):
    def reflected_schema(self, table):
        connection = Mock(dialect=mysql.dialect())
        inspector = Mock()
        inspector.get_table_names.return_value = [table.name]
        inspector.get_columns.return_value = [dict(name=c.name, type=c.type.dialect_impl(connection.dialect)) for c in table.columns]
        inspector.get_pk_constraint.return_value = {'constrained_columns': [c.name for c in table.primary_key]}
        inspector.get_table_options.return_value = {'mysql_engine': 'InnoDB'}
        inspector.get_indexes.return_value = [{'column_names': ['source', 'academic_year', 'semester', 'source_event_id'], 'unique': True}]
        inspector.get_unique_constraints.return_value = []
        return connection, inspector

    def test_history_requires_transactional_storage_and_primary_key(self):
        for table in (CalendarCrawlRun.__table__, CalendarCrawlLock.__table__):
            connection, inspector = self.reflected_schema(table)
            with patch('crawler.academic_calendar.repository.inspect', return_value=inspector):
                check_schema(connection, [table])
                inspector.get_table_options.return_value = {'mysql_engine': 'MyISAM'}
                with self.assertRaisesRegex(SchemaError, 'InnoDB'):
                    check_schema(connection, [table])
                inspector.get_table_options.return_value = {'mysql_engine': 'InnoDB'}
                inspector.get_pk_constraint.return_value = {'constrained_columns': []}
                with self.assertRaisesRegex(SchemaError, '기본 키'):
                    check_schema(connection, [table])

    def test_timestamp_and_unbounded_text_do_not_match_spring_contract(self):
        table = SchoolCalendarEvent.__table__
        for column_name, incompatible_type in [('created_at', mysql.TIMESTAMP(fsp=6)), ('title', mysql.TEXT()),
                                                ('title', mysql.VARCHAR(100)), ('active', mysql.BIT(8))]:
            connection, inspector = self.reflected_schema(table)
            for col in inspector.get_columns.return_value:
                if col['name'] == column_name:
                    col['type'] = incompatible_type
            with self.subTest(column=column_name), patch('crawler.academic_calendar.repository.inspect', return_value=inspector):
                with self.assertRaises(SchemaError):
                    check_schema(connection, [table])


if __name__ == '__main__':
    unittest.main()
