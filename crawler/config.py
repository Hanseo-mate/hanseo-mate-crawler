
import os
import importlib.util


BASE_URL = "https://www.hanseo.ac.kr"
REQUEST_DELAY_SECONDS = 1
REQUEST_TIMEOUT = (10, 30)
HTML_PARSER = "html5lib"


# 운영 / 테스트 환경 구분
TEST_ENV_KEYS = (
    "CRAWLER_TEST_DB_HOST",
    "CRAWLER_TEST_DB_USER",
    "CRAWLER_TEST_DB_PASSWORD",
    "CRAWLER_TEST_DB_NAME",
    "CRAWLER_TEST_IMAGE_DIR",
    "CRAWLER_TEST_IMAGE_BASE_URL",
)

CRAWLER_ENV = os.getenv("CRAWLER_ENV", "production").lower()

if CRAWLER_ENV not in ("production", "test"):
    raise RuntimeError(f"Invalid crawler environment: {CRAWLER_ENV}")

IS_TEST = CRAWLER_ENV == "test" or any(
    key in os.environ for key in TEST_ENV_KEYS
)


# 테스트 환경변수 검증
if IS_TEST:
    missing = [
        key for key in TEST_ENV_KEYS
        if not os.getenv(key)
    ]

    if missing:
        raise RuntimeError(
            "Missing test crawler environment variables: "
            + ", ".join(missing)
        )

    if os.environ["CRAWLER_TEST_DB_NAME"] != "hanseo_mate_test":
        raise RuntimeError(
            "Test crawler must use hanseo_mate_test database"
        )


# 이미지 저장 설정
if IS_TEST:
    IMAGE_SAVE_DIRECTORY = os.environ["CRAWLER_TEST_IMAGE_DIR"]
    IMAGE_BASE_URL = os.environ["CRAWLER_TEST_IMAGE_BASE_URL"]
else:
    IMAGE_SAVE_DIRECTORY = "/home/hanseo-mate/images"
    IMAGE_BASE_URL = "http://34.64.250.12/images/"


# 식당별 고정 식단표 URL
# food_area=1 서산 학생식당
# food_area=2 서산 교직원식당
# food_area=3 태안 학생식당
# food_area=4 태안 교직원식당
CAFETERIA_URLS = {
    "MAIN_STUDENT": f"{BASE_URL}/food/foodView.do?food_area=1&s=hs&m=0504",
    "MAIN_STAFF": f"{BASE_URL}/food/foodView.do?food_area=2&s=hs&m=0504",
    "TAEAN_STUDENT": f"{BASE_URL}/food/foodView.do?food_area=3&s=hs&m=0504",
    "TAEAN_STAFF": f"{BASE_URL}/food/foodView.do?food_area=4&s=hs&m=0504",
}


# MySQL 연결 설정
if IS_TEST:
    DB_CONFIG = {
        "host": os.environ["CRAWLER_TEST_DB_HOST"],
        "user": os.environ["CRAWLER_TEST_DB_USER"],
        "password": os.environ["CRAWLER_TEST_DB_PASSWORD"],
        "database": os.environ["CRAWLER_TEST_DB_NAME"],
        "charset": "utf8mb4",
        "autocommit": False,
    }
else:
    DB_CONFIG = {
        "host": "127.0.0.1",
        "user": "root",
        "password": "1234",
        "database": "hanseo_mate",
        "charset": "utf8mb4",
        "autocommit": False,
    }


def resolve_html_parser() -> str:
    preferred = HTML_PARSER.strip().lower()

    if preferred == "html5lib":
        if importlib.util.find_spec("html5lib") is not None:
            return "html5lib"
        if importlib.util.find_spec("lxml") is not None:
            return "lxml"
        return "html.parser"

    if preferred == "lxml":
        if importlib.util.find_spec("lxml") is not None:
            return "lxml"
        if importlib.util.find_spec("html5lib") is not None:
            return "html5lib"
        return "html.parser"

    return "html.parser"


ACTIVE_HTML_PARSER = resolve_html_parser()
