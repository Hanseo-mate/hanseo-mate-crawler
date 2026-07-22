from ..models import BoardDefinition
from .base import HanseoNoticeCrawler


ACADEMIC_NOTICE_BOARD = BoardDefinition(
    key="academic",
    name="학사공지",
    board_id=298,
    menu_code="040101",
)


def build_crawler() -> HanseoNoticeCrawler:
    return HanseoNoticeCrawler(ACADEMIC_NOTICE_BOARD)
