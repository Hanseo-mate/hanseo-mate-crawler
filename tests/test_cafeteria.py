import unittest
from datetime import date
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from crawler.cafeteria.parser import parse_cafeteria_menu
from crawler.cafeteria.service import crawl_and_save_cafeteria, serialize_daily_menus
from crawler.models import Base, DailyMenu, MealSection, MealTime, RestaurantType


HTML = """
<div class="fd_info"><p class="txt">2026.08.31</p></div>
<div class="fd_table"><table><tbody><tr>
  <td>월</td>
  <td>1코너 (5.5)<br>쌀밥<br>불고기<br>---<br>A 코너 5,500원<br>돈가스<br>---<br>특식 5500<br>비빔밥</td>
  <td></td>
</tr></tbody></table></div>
"""


class CafeteriaParserTest(unittest.TestCase):
    def test_parses_flexible_corners_prices_and_raw_text(self) -> None:
        menus = parse_cafeteria_menu(HTML, RestaurantType.MAIN_STUDENT)
        sections = menus[0].meal_sections

        self.assertEqual([section.corner_name for section in sections], ["1코너", "A코너", "특식"])
        self.assertEqual([section.price for section in sections], [5500, 5500, 5500])
        self.assertEqual(
            [section.dishes for section in sections],
            [["쌀밥", "불고기"], ["돈가스"], ["비빔밥"]],
        )
        self.assertEqual(sections[0].raw_text, "1코너 (5.5)\n쌀밥\n불고기")


class CafeteriaSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.response = Mock(text=HTML, apparent_encoding="utf-8", encoding="utf-8")
        self.response.raise_for_status.return_value = None

    def test_replaces_restaurant_history_then_aborts_identical_update(self) -> None:
        with self.session_factory() as session:
            old_menu = DailyMenu(restaurant_type=RestaurantType.MAIN_STUDENT, menu_date=date(2026, 8, 24))
            old_menu.meal_sections.append(
                MealSection(
                    meal_time=MealTime.LUNCH,
                    corner_name="1코너",
                    price=None,
                    dishes=["이전 메뉴"],
                    raw_text="이전 메뉴",
                )
            )
            other_restaurant = DailyMenu(
                restaurant_type=RestaurantType.MAIN_STAFF,
                menu_date=date(2026, 8, 24),
            )
            other_restaurant.meal_sections.append(
                MealSection(
                    meal_time=MealTime.LUNCH,
                    corner_name="1코너",
                    price=None,
                    dishes=["교직원 메뉴"],
                    raw_text="교직원 메뉴",
                )
            )
            session.add_all([old_menu, other_restaurant])
            session.commit()

        with patch("crawler.cafeteria.service.requests.get", return_value=self.response):
            with self.session_factory() as session:
                first = crawl_and_save_cafeteria(
                    "https://example.test/menu",
                    RestaurantType.MAIN_STUDENT,
                    session,
                )

            first_json = serialize_daily_menus(first.menus)
            self.assertTrue(first.updated)

            with self.session_factory() as session:
                student_dates = [
                    menu.menu_date.isoformat()
                    for menu in session.query(DailyMenu)
                    .filter(DailyMenu.restaurant_type == RestaurantType.MAIN_STUDENT)
                    .all()
                ]
                staff_count = (
                    session.query(DailyMenu)
                    .filter(DailyMenu.restaurant_type == RestaurantType.MAIN_STAFF)
                    .count()
                )
                second = crawl_and_save_cafeteria(
                    "https://example.test/menu",
                    RestaurantType.MAIN_STUDENT,
                    session,
                )

        self.assertEqual(student_dates, ["2026-08-31"])
        self.assertEqual(staff_count, 1)
        self.assertFalse(second.updated)
        self.assertEqual(serialize_daily_menus(second.menus), first_json)


if __name__ == "__main__":
    unittest.main()