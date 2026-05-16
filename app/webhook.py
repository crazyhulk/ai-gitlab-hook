from datetime import date
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from . import state
from .handlers import handle_issue_event, handle_mr_event, handle_note_event, handle_push_event
from .logger import get_logger
from .wechat import send_webhook

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


@router.get("/check-hotfix-sync")
async def check_hotfix_sync() -> dict:
    """检查热修合入 main 后超时未同步 pre 的记录，发送企微告警并写入违规记录。

    建议通过定时任务每 1–2 小时调用一次（如 cron 或 Celery beat）。
    """
    config = state.get_config()
    overdue = state.list_overdue_hotfix_syncs(config.hotfix_sync_threshold_hours)
    if not overdue:
        return {"total": 0, "items": []}

    # 按 project_id 分组，每个项目发一条聚合告警
    by_project: dict[int, list[dict]] = {}
    for item in overdue:
        pid = item["project_id"]
        by_project.setdefault(pid, []).append(item)

    alerted: list[dict] = []
    for project_id, items in by_project.items():
        project_name = items[0]["project"]
        mr_lines = "\n".join(
            f"> - MR !{it['mr_iid']}：[{it['mr_title']}]({it['mr_url']}) "
            f"（{it['operator_name']}，合入时间：{it['created_at']}）"
            for it in items
        )
        content = (
            f"### ⏰ 热修代码未及时同步 pre\n"
            f"> **项目**：{project_name}\n"
            f"> 以下热修 MR 合入 `main` 已超过 **{config.hotfix_sync_threshold_hours} 小时**，"
            f"尚未同步到 `pre`：\n"
            f"{mr_lines}\n\n"
            f"**下一步**\n"
            f"> 请执行以下命令将热修代码同步到 pre：\n"
            f"> `ccg gitlab mr sync-pre`"
        )
        at_mobiles = config.tl_mobiles
        logger.warning(
            "Hotfix sync overdue project=%s mr_count=%s threshold_hours=%s",
            project_name, len(items), config.hotfix_sync_threshold_hours,
        )
        state.record_violation(
            operator="",
            operator_name="",
            violation_type="hotfix_sync_overdue",
            description=(
                f"热修合入 main 超过 {config.hotfix_sync_threshold_hours} 小时未同步 pre，"
                f"涉及 MR: {[it['mr_iid'] for it in items]}"
            ),
            project=project_name,
            detail={"project_id": project_id, "mr_iids": [it["mr_iid"] for it in items]},
        )
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        alerted.extend(items)

    return {"total": len(alerted), "items": alerted}


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
