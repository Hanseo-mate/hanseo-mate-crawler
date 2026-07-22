import logging
import sys

from .config import ACTIVE_HTML_PARSER, HTML_PARSER
from .runtime import configure_logging
from .service import crawl_service


def main() -> None:
    configure_logging()
    if ACTIVE_HTML_PARSER != HTML_PARSER:
        logging.warning(
            "요청 파서(%s)를 사용할 수 없어 대체 파서(%s)를 사용합니다. python=%s",
            HTML_PARSER,
            ACTIVE_HTML_PARSER,
            sys.executable,
        )
    else:
        logging.info("HTML 파서=%s, python=%s", ACTIVE_HTML_PARSER, sys.executable)

    try:
        crawl_service.run_crawlers()
    except Exception as exc:
        logging.exception("크롤러 실행 실패: %s", exc)
        raise SystemExit(1) from exc
