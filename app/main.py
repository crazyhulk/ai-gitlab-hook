import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import state
from .config import load_config
from .handlers import run_hotfix_sync_check
from .logger import get_logger, set_request_id, set_request_path, setup_logging
from .webhook import router as webhook_router

logger = get_logger(__name__)


async def _hotfix_sync_loop() -> None:
    config = state.get_config()
    interval = config.hotfix_sync_check_interval_seconds
    logger.info("Hotfix sync checker started interval=%ss threshold=%sh", interval, config.hotfix_sync_threshold_hours)
    while True:
        await asyncio.sleep(interval)
        try:
            logger.info("Hotfix sync check: scanning overdue records (threshold=%sh)", config.hotfix_sync_threshold_hours)
            alerted = run_hotfix_sync_check(state.get_config())
            logger.info("Hotfix sync check: done alerted=%s", len(alerted))
        except Exception as exc:
            logger.exception("Hotfix sync check failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    setup_logging(config.log.level, config.log.dir, config.log.file)
    state.set_config(config)
    logger.info(
        "Service started log_level=%s log_dir=%s log_file=%s "
        "wechat_configured=%s gitlab_secret_configured=%s",
        config.log.level,
        config.log.dir,
        config.log.file,
        bool(config.wechat.webhook_url),
        bool(config.gitlab.secret_token),
    )
    task = asyncio.create_task(_hotfix_sync_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Service shutting down")


app = FastAPI(title="GitLab Hook Service", lifespan=lifespan)
app.include_router(webhook_router)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    set_request_id(request_id)
    set_request_path(request.url.path)

    start = time.perf_counter()
    body_bytes = await request.body()

    logger.info(
        "→ %s %s query=%s",
        request.method,
        request.url.path,
        str(request.query_params) or "-",
    )
    if body_bytes:
        logger.debug("Request payload: %s", body_bytes.decode("utf-8", errors="replace"))

    async def _receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    patched_request = Request(request.scope, _receive)

    try:
        response: Response = await call_next(patched_request)
    except Exception as exc:
        logger.exception("Unhandled exception: %s", exc)
        err_body = json.dumps({"error": "internal server error", "request_id": request_id})
        return Response(content=err_body, status_code=500, media_type="application/json")

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    resp_body = b"".join(chunks)

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("← %d elapsed_ms=%.1f", response.status_code, elapsed_ms)
    logger.debug("Response body: %s", resp_body.decode("utf-8", errors="replace"))

    return Response(
        content=resp_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
