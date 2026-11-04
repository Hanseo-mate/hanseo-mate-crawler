from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import Date, DateTime, Integer, JSON, String, inspect, or_, select, text, update
from sqlalchemy.dialects.mysql import BIT, DATETIME

from .models import CalendarCrawlLock, CalendarCrawlRun, SchoolCalendarEvent
from .parser import CalendarValidationError

LEASE_SECONDS = 300
ACTIVE_STATUSES = ("starting", "running")


class AlreadyRunning(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class SchemaError(RuntimeError):
    pass


def db_now(session):
    if session.bind.dialect.name == "mysql":
        return session.execute(text("SELECT UTC_TIMESTAMP(6)")).scalar_one()
    return datetime.fromisoformat(session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one())


def db_clock(session):
    return text("UTC_TIMESTAMP(6)" if session.bind.dialect.name == "mysql" else "CURRENT_TIMESTAMP")


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def check_schema(connection, tables):
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table in tables:
        if table.name not in existing:
            raise SchemaError(f"필수 테이블 없음: {table.name}. Spring 0005 SQL 및 Python 이력 migration을 확인하세요.")
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        missing = set(table.columns.keys()) - columns.keys()
        if missing:
            raise SchemaError(f"{table.name} 필수 컬럼 없음: {sorted(missing)}")
        primary_key = inspector.get_pk_constraint(table.name).get('constrained_columns', [])
        if primary_key != [c.name for c in table.primary_key]:
            raise SchemaError(f"{table.name}: 기본 키 불일치")
        if connection.dialect.name == "mysql":
            if inspector.get_table_options(table.name).get("mysql_engine", "").lower() != "innodb":
                raise SchemaError(f"{table.name}: 트랜잭션/행 잠금에 InnoDB가 필요합니다.")
            for col in table.columns:
                actual = columns[col.name]['type']
                expected_length = getattr(col.type, 'length', None)
                actual_length = getattr(actual, 'length', None)
                if expected_length and (not isinstance(actual, String) or not actual_length or actual_length < expected_length):
                    raise SchemaError(f"{table.name}.{col.name}: 컬럼 길이가 계약보다 짧습니다.")
                if isinstance(col.type, DateTime) and (not isinstance(actual, DATETIME) or getattr(actual, 'fsp', 0) != 6):
                    raise SchemaError(f"{table.name}.{col.name}: UTC DATETIME(6) 필요")
                if isinstance(col.type, Date) and not isinstance(col.type, DateTime) and (not isinstance(actual, Date) or isinstance(actual, DateTime)):
                    raise SchemaError(f"{table.name}.{col.name}: DATE 필요")
                if isinstance(col.type, Integer) and not isinstance(actual, Integer):
                    raise SchemaError(f"{table.name}.{col.name}: 정수 타입 필요")
                if isinstance(col.type, JSON) and not isinstance(actual, JSON):
                    raise SchemaError(f"{table.name}.{col.name}: JSON 타입 필요")
            if table.name == 'school_calendar_events':
                active_type = columns['active']['type']
                if not isinstance(active_type, BIT) or active_type.length != 1:
                    raise SchemaError("school_calendar_events.active: BIT(1) 필요")
    if SchoolCalendarEvent.__table__ in tables:
        indexes = inspector.get_indexes("school_calendar_events") + inspector.get_unique_constraints("school_calendar_events")
        expected = ["source", "academic_year", "semester", "source_event_id"]
        if not any(i['column_names'] == expected and i.get('unique', True) for i in indexes):
            raise SchemaError("school_calendar_events 원본 유일 인덱스 없음: Spring 0005 SQL 적용 필요")


def blank_result(year, semester):
    from .parser import source_url
    return dict(academic_year=year, semester=semester, status="pending", source_url=source_url(year, semester),
                inserted=0, updated=0, unchanged=0, deactivated=0, event_count=0,
                started_at=None, finished_at=None, error=None, preservation_reason=None)


class CalendarRepository:
    def __init__(self, session_factory):
        self.sessions = session_factory

    def _prepare(self, session):
        if session.bind.dialect.name == "mysql":
            session.execute(text("SET SESSION innodb_lock_wait_timeout = 2"))
            session.execute(text("SET SESSION sql_mode = CONCAT_WS(',', @@SESSION.sql_mode, 'STRICT_ALL_TABLES')"))

    def _interrupt_abandoned(self, session, now):
        # Current locking read is required under MySQL REPEATABLE READ: a claim
        # may have observed the lock before another transaction finished.
        for run in session.scalars(select(CalendarCrawlRun).where(
                CalendarCrawlRun.status.in_(ACTIVE_STATUSES)).with_for_update()):
            run.status = "failed"
            run.finished_at = now
            run.error = "실행 lease 만료/프로세스 중단. 완료된 학기 결과는 보존합니다."
            results = deepcopy(run.results)
            for result in results.values():
                if result['status'] in ('pending', 'running'):
                    result.update(status="failed", finished_at=iso(now), error=run.error,
                                  preservation_reason="중단된 학기의 미반영 데이터 보존")
            run.results = results

    def claim(self, year, semesters):
        run_id = uuid.uuid4().hex
        with self.sessions() as session, session.begin():
            self._prepare(session)
            check_schema(session.connection(), [CalendarCrawlLock.__table__, CalendarCrawlRun.__table__])
            now = db_now(session)
            # A non-locking read returns 409 promptly while another worker commits
            # a semester. The conditional UPDATE below still arbitrates races.
            current = session.get(CalendarCrawlLock, 1)
            if current is None:
                raise SchemaError("학사일정 실행 잠금 행 없음: python -m crawler.academic_calendar.migrate 실행 필요")
            if current.owner_run_id and current.expires_at and current.expires_at > now:
                raise AlreadyRunning("학사일정 수집이 이미 실행 중입니다.")
            claimed = session.execute(update(CalendarCrawlLock).where(
                CalendarCrawlLock.id == 1,
                or_(CalendarCrawlLock.owner_run_id.is_(None), CalendarCrawlLock.expires_at <= db_clock(session))
            ).values(owner_run_id=run_id, expires_at=now + timedelta(seconds=LEASE_SECONDS)))
            if claimed.rowcount != 1:
                if session.get(CalendarCrawlLock, 1) is None:
                    raise SchemaError("학사일정 실행 잠금 행 없음: python -m crawler.academic_calendar.migrate 실행 필요")
                raise AlreadyRunning("학사일정 수집이 이미 실행 중입니다.")
            self._interrupt_abandoned(session, now)
            session.add(CalendarCrawlRun(run_id=run_id, academic_year=year, semesters=list(semesters),
                        status="starting", started_at=now,
                        results={str(s): blank_result(year, s) for s in semesters}))
        return run_id

    def fence(self, session, run_id):
        """Hold the lease row until commit, fencing even a worker returning after expiry."""
        self._prepare(session)
        now = db_now(session)
        result = session.execute(update(CalendarCrawlLock).where(
            CalendarCrawlLock.id == 1, CalendarCrawlLock.owner_run_id == run_id,
            CalendarCrawlLock.expires_at > db_clock(session)
        ).values(expires_at=now + timedelta(seconds=LEASE_SECONDS)))
        if result.rowcount != 1:
            raise LeaseLost("실행 잠금 소유권/유효기간을 잃었습니다. 기존 데이터는 보존합니다.")
        return now

    def begin_semester(self, run_id, semester):
        with self.sessions() as session, session.begin():
            now = self.fence(session, run_id)
            run = session.get(CalendarCrawlRun, run_id)
            results = deepcopy(run.results)
            results[str(semester)].update(status="running", started_at=iso(now))
            run.status, run.results = "running", results

    def record_result(self, session, run_id, semester, **changes):
        run = session.get(CalendarCrawlRun, run_id)
        results = deepcopy(run.results)
        results[str(semester)].update(changes)
        run.results = results

    def fail_semester(self, run_id, semester, error):
        with self.sessions() as session, session.begin():
            now = self.fence(session, run_id)
            run = session.get(CalendarCrawlRun, run_id)
            # A COMMIT acknowledgement can be lost after MySQL made the transaction
            # durable. Its atomic success record is stronger evidence than the error.
            if run.results[str(semester)]['status'] in ('completed', 'unpublished'):
                return False
            self.record_result(session, run_id, semester, status="failed", error=error,
                               finished_at=iso(now), preservation_reason="수집/검증/저장 실패로 기존 데이터 보존")
        return True

    def apply(self, run_id, snapshot):
        with self.sessions() as session, session.begin():
            now = self.fence(session, run_id)
            check_schema(session.connection(), [SchoolCalendarEvent.__table__])
            if not snapshot.events:
                self.record_result(session, run_id, snapshot.semester, status="unpublished", finished_at=iso(now),
                                   preservation_reason="완전한 월 영역에 일정 0건: 미게시로 취급하여 기존 데이터 보존")
                return
            existing = {row.source_event_id: row for row in session.scalars(select(SchoolCalendarEvent).where(
                SchoolCalendarEvent.source == "HANSEO", SchoolCalendarEvent.academic_year == snapshot.academic_year,
                SchoolCalendarEvent.semester == snapshot.semester))}
            counts = dict(inserted=0, updated=0, unchanged=0, deactivated=0)
            present = {event.source_event_id for event in snapshot.events}
            types = {event.event_type for event in snapshot.events}
            missing_core = {row.event_type for row in existing.values() if row.active and row.event_type != "OTHER"} - types
            if missing_core:
                # Mixing retained old exams with new IDs can create active duplicates.
                # Preserve the entire last good semester when completeness is uncertain.
                raise CalendarValidationError(f"기존 핵심 일정 종류 누락({', '.join(sorted(missing_core))}): 학기 반영 보류")
            for event in snapshot.events:
                values = dict(start_date=event.start_date, end_date=event.end_date, title=event.title,
                              event_type=event.event_type, source_url=snapshot.source_url, active=True)
                row = existing.get(event.source_event_id)
                if row is None:
                    row = SchoolCalendarEvent(source="HANSEO", academic_year=snapshot.academic_year,
                        semester=snapshot.semester, source_event_id=event.source_event_id,
                        created_at=now, updated_at=now, **values)
                    session.add(row)
                    counts['inserted'] += 1
                elif any(getattr(row, key) != value for key, value in values.items()):
                    for key, value in values.items():
                        setattr(row, key, value)
                    row.updated_at = now
                    counts['updated'] += 1
                else:
                    counts['unchanged'] += 1
                row.last_synced_at = now
            for event_id, row in existing.items():
                if event_id not in present and row.active:
                    row.active, row.updated_at = False, now
                    counts['deactivated'] += 1
            session.flush()
            # Result and all semester changes are committed together.
            self.record_result(session, run_id, snapshot.semester, status="completed", finished_at=iso(now),
                               event_count=len(snapshot.events), **counts)

    def finish(self, run_id, fatal_error=None):
        with self.sessions() as session, session.begin():
            now = self.fence(session, run_id)
            run = session.get(CalendarCrawlRun, run_id)
            results = deepcopy(run.results)
            for result in results.values():
                if result['status'] in ('pending', 'running'):
                    result.update(status="failed", error=fatal_error or "작업이 완료되지 않았습니다.", finished_at=iso(now))
            statuses = [r['status'] for r in results.values()]
            if all(s == 'failed' for s in statuses):
                status = 'failed'
            elif 'failed' in statuses:
                status = 'partial_failed'
            elif all(s == 'unpublished' for s in statuses):
                status = 'unpublished'
            elif 'unpublished' in statuses:
                status = 'partial_unpublished'
            else:
                status = 'completed'
            run.status, run.results, run.finished_at, run.error = status, results, now, fatal_error
            session.execute(update(CalendarCrawlLock).where(CalendarCrawlLock.id == 1).values(owner_run_id=None, expires_at=None))

    def get_state(self, run_id=None):
        with self.sessions() as session, session.begin():
            self._prepare(session)
            check_schema(session.connection(), [CalendarCrawlLock.__table__, CalendarCrawlRun.__table__])
            current = session.get(CalendarCrawlLock, 1)
            if current is None:
                raise SchemaError("학사일정 실행 잠금 행 없음: Python 이력 migration을 확인하세요.")
            if current and current.owner_run_id and current.expires_at and current.expires_at <= db_now(session):
                recovered = session.execute(update(CalendarCrawlLock).where(
                    CalendarCrawlLock.id == 1, CalendarCrawlLock.owner_run_id.is_not(None),
                    CalendarCrawlLock.expires_at <= db_clock(session)
                ).values(owner_run_id=None, expires_at=None))
                if recovered.rowcount:
                    self._interrupt_abandoned(session, db_now(session))
            query = select(CalendarCrawlRun)
            query = query.where(CalendarCrawlRun.run_id == run_id) if run_id else query.order_by(CalendarCrawlRun.started_at.desc()).limit(1)
            run = session.scalars(query).first()
            if not run:
                return None if run_id else {"status": "idle", "run_id": None}
            return dict(run_id=run.run_id, academic_year=run.academic_year, semesters=run.semesters,
                        status=run.status, started_at=iso(run.started_at), finished_at=iso(run.finished_at),
                        error=run.error, results=deepcopy(run.results))
