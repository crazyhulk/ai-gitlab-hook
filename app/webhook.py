from datetime import date
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from . import state
from .handlers import handle_issue_event, handle_mr_event, handle_note_event, handle_push_event
from .logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/violations")
async def get_violations(
    start: str = Query(default=str(date.today()), description="开始日期 YYYY-MM-DD"),
    end: str = Query(default=str(date.today()), description="结束日期 YYYY-MM-DD"),
) -> dict:
    records = state.list_violations(start, end)
    return {"total": len(records), "items": records}


@router.post("/gitlab/webhook")
async def gitlab_webhook(
    request: Request,
    x_gitlab_event: str = Header(default=""),
    x_gitlab_token: str = Header(default=""),
) -> dict[str, str]:
    config = state.get_config()

    if config.gitlab.secret_token and x_gitlab_token != config.gitlab.secret_token:
        logger.warning("Rejected request: invalid GitLab webhook token")
        raise HTTPException(status_code=401, detail="Invalid token")

    payload: dict[str, Any] = await request.json()

    logger.info("GitLab webhook received event=%s", x_gitlab_event)

    if x_gitlab_event == "Issue Hook":
        result = handle_issue_event(payload, config)
    elif x_gitlab_event == "Merge Request Hook":
        result = handle_mr_event(payload, config)
    elif x_gitlab_event == "Note Hook":
        result = handle_note_event(payload, config)
    elif x_gitlab_event == "Push Hook":
        result = handle_push_event(payload, config)
    else:
        logger.info("Unhandled event type=%s, ignored", x_gitlab_event)
        result = "ignored"

    return {"status": result}
