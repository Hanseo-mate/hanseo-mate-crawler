"""Separate metadata: never create/alter Spring's table from application startup."""
from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import BIT, DATETIME
from sqlalchemy.orm import declarative_base

CalendarBase = declarative_base()
UTC_DATETIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")


class SchoolCalendarEvent(CalendarBase):
    __tablename__ = "school_calendar_events"
    __table_args__ = (UniqueConstraint("source", "academic_year", "semester", "source_event_id", name="uk_school_calendar_source"),)
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    title = Column(String(500), nullable=False)
    created_at = Column(UTC_DATETIME, nullable=False)
    updated_at = Column(UTC_DATETIME, nullable=False)
    source = Column(String(20), nullable=False)
    source_event_id = Column(String(100))
    academic_year = Column(Integer)
    semester = Column(Integer)
    event_type = Column(String(30), nullable=False)
    source_url = Column(String(1000))
    last_synced_at = Column(UTC_DATETIME)
    active = Column(Boolean().with_variant(BIT(1), "mysql"), nullable=False)


class CalendarCrawlRun(CalendarBase):
    __tablename__ = "academic_calendar_crawl_runs"
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    run_id = Column(String(32), primary_key=True)
    academic_year = Column(Integer, nullable=False)
    semesters = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False)
    started_at = Column(UTC_DATETIME, nullable=False)
    finished_at = Column(UTC_DATETIME)
    results = Column(JSON, nullable=False)
    error = Column(Text)


class CalendarCrawlLock(CalendarBase):
    __tablename__ = "academic_calendar_crawl_lock"
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    id = Column(Integer, primary_key=True)
    owner_run_id = Column(String(32))
    expires_at = Column(UTC_DATETIME)
