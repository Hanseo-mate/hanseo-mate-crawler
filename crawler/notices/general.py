from ..models import BoardDefinition
from .base import HanseoNoticeCrawler


GENERAL_NOTICE_BOARD = BoardDefinition(
    key="general",
    name="일반공지",
    board_id=299,
    menu_code="040102",
)


def build_crawler() -> HanseoNoticeCrawler:
    return HanseoNoticeCrawler(GENERAL_NOTICE_BOARD)
