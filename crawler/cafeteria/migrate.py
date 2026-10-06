"""배포 시 식당별 수집 기록 테이블을 추가한다. 기존 식단 테이블은 변경하지 않는다."""

from sqlalchemy import inspect

from ..database import get_sqlalchemy_engine
from ..models import CafeteriaCrawlProgress


def main() -> None:
    engine = get_sqlalchemy_engine()
    table = CafeteriaCrawlProgress.__table__
    table.create(bind=engine, checkfirst=True)
    columns = {column["name"] for column in inspect(engine).get_columns(table.name)}
    missing = set(table.columns.keys()) - columns
    if missing:
        raise RuntimeError(f"{table.name}: missing columns: {sorted(missing)}")
    print(f"Migration OK: {table.name}")


if __name__ == "__main__":
    main()
