from ..models import BoardDefinition
from .base import HanseoNoticeCrawler


GRADUATE_NOTICE_BOARD = BoardDefinition(
    key="graduate",
    name="대학원공지",
    board_id=302,
    menu_code="040105",
)


def build_crawler() -> HanseoNoticeCrawler:
    return HanseoNoticeCrawler(GRADUATE_NOTICE_BOARD)