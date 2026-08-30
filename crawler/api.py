from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from requests.exceptions import InvalidURL

from .cafeteria.service import cafeteria_crawl_service
from .config import CAFETERIA_URLS
from .models import RestaurantType
from .service import crawl_service


class CrawlTriggerRequest(BaseModel):
    notice_types: list[str] | None = Field(default=None)
    mode: Literal["background", "sync"] = "background"


class CafeteriaCrawlTriggerRequest(BaseModel):
    restaurant_types: list[RestaurantType] | None = Field(
        default=None,
        description="크롤링할 식당 타입 목록. 생략 시 전체 식당 크롤링",
    )
    mode: Literal["background", "sync"] = "background"


app = FastAPI(title="Hanseo Mate Crawler API", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "available_notice_types": crawl_service.get_available_notice_types(),
        "available_restaurant_types": [restaurant_type.value for restaurant_type in RestaurantType],
    }


@app.get("/crawl/status")
def crawl_status() -> dict:
    return crawl_service.get_state()


@app.post("/crawl/run")
def trigger_crawl(request: CrawlTriggerRequest) -> dict:
    try:
        if request.mode == "sync":
            crawl_service.run_crawlers(request.notice_types)
            return crawl_service.get_state()

        return crawl_service.start_background_run(request.notice_types)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/cafeteria-crawl/status")
def cafeteria_crawl_status() -> dict:
    return cafeteria_crawl_service.get_state()


@app.post("/cafeteria-crawl/run")
def trigger_cafeteria_crawl(request: CafeteriaCrawlTriggerRequest) -> dict:
    # 요청된 식당 타입에 대해 URL 매핑 구성
    rest_types = request.restaurant_types or list(RestaurantType)
    targets: list[tuple[str, RestaurantType]] = []
    missing = []
    for rt in rest_types:
        url = CAFETERIA_URLS.get(rt.value)
        if not url:
            missing.append(rt.value)
        else:
            targets.append((url, rt))

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"다음 식당 타입에 대한 URL이 설정되지 않았습니다: {', '.join(missing)}",
        )

    try:
        if request.mode == "sync":
            cafeteria_crawl_service.run_crawlers(targets)
            return cafeteria_crawl_service.get_state()

        return cafeteria_crawl_service.start_background_run(targets)
    except (ValueError, InvalidURL) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
