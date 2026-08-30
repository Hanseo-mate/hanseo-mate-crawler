import importlib.util


BASE_URL = "https://www.hanseo.ac.kr"
REQUEST_DELAY_SECONDS = 1
REQUEST_TIMEOUT = (10, 30)
HTML_PARSER = "html5lib"
IMAGE_SAVE_DIRECTORY = "/home/hanseo-mate/images"
IMAGE_BASE_URL = "http://34.64.250.12/images/"

# 식당별 기본 식단표 URL (HTML select option value 기준)
# value="1" 학생식당 / value="2" 교직원식당 / value="3" 태안-학생식당 / value="4" 태안-교직원식당
CAFETERIA_URLS = {
    "MAIN_STUDENT":  f"{BASE_URL}/food/foodView.do?food_area=1&s=hs&m=0504",
    "MAIN_STAFF":    f"{BASE_URL}/food/foodView.do?food_area=2&s=hs&m=0504",
    "TAEAN_STUDENT": f"{BASE_URL}/food/foodView.do?food_area=3&s=hs&m=0504",
    "TAEAN_STAFF":   f"{BASE_URL}/food/foodView.do?food_area=4&s=hs&m=0504",
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
