import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from ..config import ACTIVE_HTML_PARSER, BASE_URL


class CalendarValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CalendarEvent:
    source_event_id: str
    title: str
    start_date: date
    end_date: date
    event_type: str


@dataclass(frozen=True)
class CalendarSnapshot:
    academic_year: int
    semester: int
    source_url: str
    events: tuple[CalendarEvent, ...]


def source_url(year: int, semester: int) -> str:
    return f"{BASE_URL}/schdList.do?m=0405&s=hs&schdYear={year}&section={semester}"


def normalized(text: str) -> str:
    return " ".join(text.split())


def classify(title: str) -> str:
    compact = re.sub(r"\s+", "", title)
    if re.fullmatch(r"(?:\d{4}학년도)?(?:[12]학기)?학기개시일", compact):
        return "SEMESTER_START"
    if re.fullmatch(r"(?:\d{4}학년도)?(?:[12]학기)?중간고사", compact):
        return "MIDTERM"
    if re.fullmatch(r"(?:\d{4}학년도)?(?:[12]학기)?기말고사", compact):
        return "FINAL"
    if re.fullmatch(r"(?:하계|동계)방학(?:시작|시작일)?", compact):
        return "VACATION_START"
    return "OTHER"


def parse_dates(text: str, year: int, semester: int, month: int) -> tuple[date, date]:
    match = re.fullmatch(
        r"(?:(\d{1,2})\s*월\s*)?(\d{1,2})\s*일"
        r"(?:\s*~\s*(?:(\d{1,2})\s*월\s*)?(\d{1,2})\s*일)?", text.strip()
    )
    if not match:
        raise CalendarValidationError(f"날짜 해석 실패: {text}")
    start_month, start_day, end_month, end_day = match.groups()
    if start_month and int(start_month) != month:
        raise CalendarValidationError(f"월 영역과 시작 월 불일치: {text}")
    start_year = year + int(semester == 2 and month in (1, 2))
    final_month = int(end_month) if end_month else month
    # Explicit month rollover crosses a year; a reversed same-month range is invalid.
    end_year = start_year + int(final_month < month)
    try:
        start = date(start_year, month, int(start_day))
        end = date(end_year, final_month, int(end_day or start_day))
    except ValueError as exc:
        raise CalendarValidationError(f"유효하지 않은 날짜: {text}") from exc
    expected_months = range(3, 9) if semester == 1 else (9, 10, 11, 12, 1, 2)
    if month not in expected_months or start > end:
        raise CalendarValidationError(f"월 영역 불일치/역전 날짜: {text}")
    # Registration and other normal events may extend past August/February.
    # Metadata belongs to the queried page, not to the end date.
    return start, end


def validate_core_events(events: tuple[CalendarEvent, ...], year: int, semester: int) -> None:
    core = {}
    term_end = date(year + (semester == 2), 9 if semester == 1 else 3, 1)
    for event in events:
        if event.event_type == "OTHER":
            continue
        if event.event_type in core:
            raise CalendarValidationError(f"핵심 일정 중복/상충: {event.event_type}")
        core[event.event_type] = event
        if event.end_date >= term_end:
            raise CalendarValidationError(f"핵심 일정이 다음 학기와 겹침: {event.source_event_id}")
        if event.event_type in ("SEMESTER_START", "VACATION_START") and event.start_date != event.end_date:
            raise CalendarValidationError(f"시작 일정이 단일 날짜가 아님: {event.event_type}")
        identity = re.fullmatch(r"(?:(\d{4})학년도)?(?:([12])학기)?(?:학기개시일|중간고사|기말고사)",
                                re.sub(r"\s+", "", event.title))
        if identity and ((identity[1] and int(identity[1]) != year) or (identity[2] and int(identity[2]) != semester)):
            raise CalendarValidationError(f"핵심 일정의 학년도/학기가 조회 조건과 불일치: {event.source_event_id}")

    start, mid, final, vacation = (core.get(t) for t in ("SEMESTER_START", "MIDTERM", "FINAL", "VACATION_START"))
    if mid and final and mid.end_date >= final.start_date:
        raise CalendarValidationError("중간고사/기말고사 기간이 겹치거나 순서가 역전됨")
    for exam in (mid, final):
        if exam and start and exam.start_date < start.start_date:
            raise CalendarValidationError("시험이 공식 학기개시일보다 먼저 시작됨")
        if exam and vacation and exam.end_date >= vacation.start_date:
            raise CalendarValidationError("시험 기간이 방학 시작과 겹치거나 방학 이후임")
    if start and vacation and start.start_date >= vacation.start_date:
        raise CalendarValidationError("방학 시작이 공식 학기개시일보다 빠르거나 같음")


def parse_calendar(html: str, year: int, semester: int, url: str) -> CalendarSnapshot:
    if not 2026 <= year <= 9998 or semester not in (1, 2) or len(url) > 1000:
        raise CalendarValidationError("학년도/학기/URL 범위 오류")
    soup = BeautifulSoup(html, ACTIVE_HTML_PARSER)
    forms = soup.select('form[name="calForm"]')
    if len(forms) != 1:
        raise CalendarValidationError("학사일정 조회 폼 누락/중복")
    form = forms[0]
    years = form.select('select[name="schdYear"] option[selected]')
    terms = form.select('input[name="section"]')
    if len(years) != 1 or years[0].get("value") != str(year):
        raise CalendarValidationError("선택한 학년도와 응답 불일치")
    if len(terms) != 1 or terms[0].get("value") != str(semester):
        raise CalendarValidationError("선택한 학기와 응답 불일치")

    expected = [3, 4, 5, 6, 7, 8] if semester == 1 else [9, 10, 11, 12, 1, 2]
    views = []
    # 실제 페이지의 두 행사 목록을 각각 검증한다. 달력 숫자는 읽지 않는다.
    for selector, cell_count in (("table.table_style02", 3), ("table.table_style03", 2)):
        tables = form.select(selector)
        if len(tables) != 1:
            raise CalendarValidationError(f"행사내용 표 누락/중복: {selector}")
        table = tables[0]
        if not any(normalized(th.get_text()) == "행사내용" for th in table.select("th")):
            raise CalendarValidationError("행사내용 헤더 누락")
        months, events = [], {}
        for row in table.select("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            if len(cells) != cell_count:
                raise CalendarValidationError("월 영역의 열 구조 변경")
            match = re.fullmatch(r"(\d{1,2})월", normalized(cells[0].get_text()))
            if not match:
                raise CalendarValidationError("월 영역 해석 실패")
            month = int(match[1])
            months.append(month)
            content = cells[-1]
            items = content.select("ul.list_blue > li")
            if len(content.select("li")) != len(items):
                raise CalendarValidationError("예상하지 못한 행사 목록 구조")
            # 목록 외 텍스트/잘린 항목을 빈 달로 취급하지 않는다.
            if normalized(content.get_text(" ")) != normalized(" ".join(i.get_text(" ") for i in items)):
                raise CalendarValidationError("행사 목록 밖의 해석되지 않은 내용")
            for item in items:
                event_id = item.get("id", "")
                if not re.fullmatch(r"date\d+", event_id) or len(event_id) > 100:
                    raise CalendarValidationError("원본 일정 ID 누락/규격 오류")
                value = normalized(item.get_text(" "))
                dates, sep, title = value.partition(":")
                title = title.strip()
                if not sep or not title or len(title) > 500:
                    raise CalendarValidationError(f"일정 제목 누락/길이 오류: {event_id}")
                start, end = parse_dates(dates, year, semester, month)
                event_type = classify(title)
                event = CalendarEvent(event_id, title, start, end, event_type)
                if event_id in events and events[event_id] != event:
                    raise CalendarValidationError(f"동일 원본 ID 내용 충돌: {event_id}")
                events[event_id] = event
        if months != expected:
            raise CalendarValidationError(f"월 영역 누락/중복/순서 불일치: {months}")
        views.append(events)
    if views[0] != views[1]:
        raise CalendarValidationError("PC/모바일 원본 ID 또는 내용 충돌: 완전 스냅샷 확인 불가")
    events = tuple(views[0].values())
    validate_core_events(events, year, semester)
    return CalendarSnapshot(year, semester, url, events)
