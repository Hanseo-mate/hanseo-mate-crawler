import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from ..config import ACTIVE_HTML_PARSER
from ..models import DailyMenu, MealSection, MealTime, RestaurantType


DATE_PATTERN = re.compile(r"(?:(?P<year>\d{4})[./-]\s*)?(?P<month>\d{1,2})[./-](?P<day>\d{1,2})")
SECTION_SEPARATOR_PATTERN = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
CORNER_PATTERN = re.compile(r"(?P<corner>(?:\d+|[A-Za-z]|[가-힣]+)\s*코너|특식)", re.IGNORECASE)
PRICE_PATTERN = re.compile(
    r"(?<![\d.])(?:(?P<decimal>\d{1,2}\.\d{1,3})|(?P<integer>\d{1,3}(?:,\d{3})+|\d{4,6}))\s*(?:원)?(?!\d)"
)


def _extract_week_start_date(soup: BeautifulSoup) -> date:
    info_node = soup.select_one("div.fd_info p.txt")
    if info_node is None:
        raise ValueError("식단 기준 날짜 정보를 찾을 수 없습니다.")

    match = DATE_PATTERN.search(info_node.get_text(" ", strip=True))
    if match is None:
        raise ValueError("식단 기준 날짜를 파싱할 수 없습니다.")

    year_text = match.group("year")
    today = date.today()
    parsed_date = date(
        int(year_text) if year_text else today.year,
        int(match.group("month")),
        int(match.group("day")),
    )
    if parsed_date.weekday() == 6:
        # 일부 식당은 기간 시작일을 월요일 전날인 일요일로 잘못 표기한다(예: 태안 학생식당).
        # weekday() 기준 역산은 이 경우 한 주 전 월요일로 되돌아가므로 다음날로 보정한다.
        return parsed_date + timedelta(days=1)
    return parsed_date - timedelta(days=parsed_date.weekday())


def _parse_price(raw_text: str) -> int | None:
    match = PRICE_PATTERN.search(raw_text)
    if match is None:
        return None

    decimal_text = match.group("decimal")
    if decimal_text is not None:
        try:
            return int(Decimal(decimal_text) * 1000)
        except InvalidOperation:
            return None

    return int(match.group("integer").replace(",", ""))


def _parse_corner_name(raw_text: str, fallback_name: str) -> str:
    match = CORNER_PATTERN.search(raw_text)
    if match is None:
        return fallback_name
    return re.sub(r"\s+", "", match.group("corner"))


def _normalize_dishes(raw_text: str) -> list[str]:
    dishes: list[str] = []
    for line in raw_text.split("\n"):
        item = line.strip()
        if not item:
            continue
        if item.startswith("("):
            continue
        if CORNER_PATTERN.search(item) or PRICE_PATTERN.search(item):
            continue

        clean_name = item.replace("*", "").strip()
        if not clean_name:
            continue

        dishes.append(clean_name)

    return dishes


def _build_meal_section(meal_time: MealTime, raw_text: str, fallback_name: str) -> MealSection | None:
    dishes = _normalize_dishes(raw_text)
    if not dishes:
        return None

    return MealSection(
        meal_time=meal_time,
        corner_name=_parse_corner_name(raw_text, fallback_name),
        price=_parse_price(raw_text),
        dishes=dishes,
        raw_text=raw_text,
    )


def _parse_cell_sections(raw_text: str, meal_time: MealTime) -> list[MealSection]:
    sections: list[MealSection] = []
    parts = [part.strip() for part in SECTION_SEPARATOR_PATTERN.split(raw_text) if part.strip()]
    for index, part in enumerate(parts, start=1):
        section = _build_meal_section(meal_time, part, f"{index}코너")
        if section is not None:
            sections.append(section)
    return sections


def parse_cafeteria_menu(html: str, rest_type: RestaurantType) -> list[DailyMenu]:
    soup = BeautifulSoup(html, ACTIVE_HTML_PARSER)
    week_start_date = _extract_week_start_date(soup)
    menus: list[DailyMenu] = []

    for index, row in enumerate(soup.select("div.fd_table table tbody tr")):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        menu_date = week_start_date + timedelta(days=index)
        daily_menu = DailyMenu(restaurant_type=rest_type, menu_date=menu_date)

        lunch_text = cells[1].get_text(separator="\n", strip=True)
        if lunch_text:
            daily_menu.meal_sections.extend(_parse_cell_sections(lunch_text, MealTime.LUNCH))

        dinner_text = cells[2].get_text(separator="\n", strip=True)
        if dinner_text:
            daily_menu.meal_sections.extend(_parse_cell_sections(dinner_text, MealTime.DINNER))

        if daily_menu.meal_sections:
            menus.append(daily_menu)

    return menus