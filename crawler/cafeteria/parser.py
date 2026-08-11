import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..config import ACTIVE_HTML_PARSER
from ..models import DailyMenu, Dish, MealSection, MealTime, MenuCategory, RestaurantType


DATE_PATTERN = re.compile(r"(?:(?P<year>\d{4})[./-]\s*)?(?P<month>\d{1,2})[./-](?P<day>\d{1,2})")


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
    return parsed_date - timedelta(days=parsed_date.weekday())


def _normalize_dishes(raw_text: str) -> list[Dish]:
    dishes: list[Dish] = []
    for line in raw_text.split("\n"):
        item = line.strip()
        if not item:
            continue
        if item.startswith("("):
            continue
        if item.startswith("---") or item.replace("-", "") == "":
            continue

        is_main_dish = item.endswith("*") or item.endswith("**")
        clean_name = item.replace("*", "").strip()
        if not clean_name:
            continue

        dishes.append(Dish(name=clean_name, is_main_dish=is_main_dish))

    return dishes


def _build_meal_section(meal_time: MealTime, menu_category: MenuCategory, raw_text: str) -> MealSection | None:
    dishes = _normalize_dishes(raw_text)
    if not dishes:
        return None

    return MealSection(
        meal_time=meal_time,
        menu_category=menu_category,
        dishes=dishes,
    )


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
            if "-------------" in lunch_text:
                lunch_parts = [part.strip() for part in lunch_text.split("-------------", 1)]
                korean_section = _build_meal_section(MealTime.LUNCH, MenuCategory.KOREAN, lunch_parts[0])
                if korean_section is not None:
                    daily_menu.meal_sections.append(korean_section)

                special_part = lunch_parts[1] if len(lunch_parts) > 1 else ""
                special_section = _build_meal_section(MealTime.LUNCH, MenuCategory.SPECIAL, special_part)
                if special_section is not None:
                    daily_menu.meal_sections.append(special_section)
            else:
                normal_lunch = _build_meal_section(MealTime.LUNCH, MenuCategory.NORMAL, lunch_text)
                if normal_lunch is not None:
                    daily_menu.meal_sections.append(normal_lunch)

        dinner_text = cells[2].get_text(separator="\n", strip=True)
        if dinner_text:
            dinner_section = _build_meal_section(MealTime.DINNER, MenuCategory.NORMAL, dinner_text)
            if dinner_section is not None:
                daily_menu.meal_sections.append(dinner_section)

        if daily_menu.meal_sections:
            menus.append(daily_menu)

    return menus