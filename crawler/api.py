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
    url: str | None = Field(default=None, description="식단표 URL. 미입력 시 식당 타입별 기본 URL 사용")
    restaurant_type: RestaurantType
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
    url = request.url or CAFETERIA_URLS.get(request.restaurant_type.value)
    if not url:
        raise HTTPException(
            status_code=400,
            detail=f"URL이 제공되지 않았고 {request.restaurant_type.value}에 대한 기본 URL도 없습니다.",
        )
    try:
        if request.mode == "sync":
            cafeteria_crawl_service.run_crawler(url, request.restaurant_type)
            return cafeteria_crawl_service.get_state()

        return cafeteria_crawl_service.start_background_run(url, request.restaurant_type)
    except (ValueError, InvalidURL) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
