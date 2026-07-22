from .academic import build_crawler as build_academic_notice_crawler
from .general import build_crawler as build_general_notice_crawler
from .graduate import build_crawler as build_graduate_notice_crawler
from .scholarship import build_crawler as build_scholarship_notice_crawler


NOTICE_CRAWLER_BUILDERS = {
    "academic": build_academic_notice_crawler,
    "general": build_general_notice_crawler,
    "scholarship": build_scholarship_notice_crawler,
    "graduate": build_graduate_notice_crawler,
}


DEFAULT_NOTICE_CRAWLER_BUILDERS = [
    NOTICE_CRAWLER_BUILDERS["academic"],
    NOTICE_CRAWLER_BUILDERS["general"],
    NOTICE_CRAWLER_BUILDERS["scholarship"],
    NOTICE_CRAWLER_BUILDERS["graduate"],
]


def get_available_notice_types() -> list[str]:
    return list(NOTICE_CRAWLER_BUILDERS.keys())


def build_crawlers(notice_types: list[str] | None = None):
    if notice_types is None:
        return [builder() for builder in DEFAULT_NOTICE_CRAWLER_BUILDERS]

    unknown_types = [notice_type for notice_type in notice_types if notice_type not in NOTICE_CRAWLER_BUILDERS]
    if unknown_types:
        raise ValueError(f"지원하지 않는 notice_type 입니다: {', '.join(unknown_types)}")

    return [NOTICE_CRAWLER_BUILDERS[notice_type]() for notice_type in notice_types]
