"""Explicit, idempotent history-only migration. Never creates Spring's event table."""
from sqlalchemy import select

from ..database import get_sqlalchemy_engine
from .models import CalendarCrawlLock, CalendarCrawlRun
from .repository import check_schema


def migrate(engine):
    tables = [CalendarCrawlLock.__table__, CalendarCrawlRun.__table__]
    for table in tables:
        table.create(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        check_schema(connection, tables)
        if connection.execute(select(CalendarCrawlLock.id).where(CalendarCrawlLock.id == 1)).first() is None:
            connection.execute(CalendarCrawlLock.__table__.insert().values(id=1))


def main():
    migrate(get_sqlalchemy_engine())
    print("Migration OK: academic_calendar_crawl_lock, academic_calendar_crawl_runs")


if __name__ == "__main__":
    main()
