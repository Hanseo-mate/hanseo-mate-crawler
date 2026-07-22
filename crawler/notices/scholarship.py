from ..models import BoardDefinition
from .base import HanseoNoticeCrawler


SCHOLARSHIP_NOTICE_BOARD = BoardDefinition(
    key="scholarship",
    name="장학공지",
    board_id=301,
    menu_code="040104",
)


def build_crawler() -> HanseoNoticeCrawler:
    return HanseoNoticeCrawler(SCHOLARSHIP_NOTICE_BOARD)
