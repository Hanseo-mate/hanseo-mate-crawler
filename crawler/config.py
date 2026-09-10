import importlib.util


BASE_URL = "https://www.hanseo.ac.kr"
REQUEST_DELAY_SECONDS = 1
REQUEST_TIMEOUT = (10, 30)
HTML_PARSER = "html5lib"
IMAGE_SAVE_DIRECTORY = "/home/hanseo-mate/images"
IMAGE_BASE_URL = "http://34.64.250.12/images/"

# 식당별 고정 식단표 URL. food_area 값만 다르고 나머지 경로는 고정이다.
# food_area=1 서산 학생식당 / 2 서산 교직원식당 / 3 태안 학생식당 / 4 태안 교직원식당
CAFETERIA_URLS = {
    "MAIN_STUDENT":  f"{BASE_URL}/food/foodView.do?food_area=1",
    "MAIN_STAFF":    f"{BASE_URL}/food/foodView.do?food_area=2",
    "TAEAN_STUDENT": f"{BASE_URL}/food/foodView.do?food_area=3",
    "TAEAN_STAFF":   f"{BASE_URL}/food/foodView.do?food_area=4",
}

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
