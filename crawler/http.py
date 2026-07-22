import logging
import time

import requests
from bs4 import BeautifulSoup

from .config import ACTIVE_HTML_PARSER, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from .models import FetchedDocument


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def fetch_document(session: requests.Session, url: str) -> FetchedDocument:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        html_text = response.text
        logging.debug("응답 수신: url=%s, html_length=%s", url, len(html_text))

        return FetchedDocument(
            soup=BeautifulSoup(html_text, ACTIVE_HTML_PARSER),
            html_text=html_text,
        )
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)
