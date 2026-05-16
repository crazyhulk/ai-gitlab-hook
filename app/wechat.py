import json
import urllib.error
import urllib.request
from typing import Optional

from .logger import get_logger

logger = get_logger(__name__)


def _post(webhook_url: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")

    logger.debug(
        "WeCom request url=%s body=%s",
        webhook_url,
        body.decode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            logger.debug("WeCom response: %s", json.dumps(result, ensure_ascii=False))
            if result.get("errcode") != 0:
                logger.error(
                    "WeCom returned error errcode=%s errmsg=%s",
                    result.get("errcode"),
                    result.get("errmsg"),
                )
            return result
    except (urllib.error.URLError, OSError) as e:
        logger.error("WeCom request failed: %s", e)
        return {"errcode": -1, "errmsg": str(e)}


def send_webhook(
    webhook_url: str,
    content: str,
    at_mobiles: Optional[list[str]] = None,
) -> None:
    if not webhook_url:
        logger.warning("WeCom webhook_url not configured, skipping notification")
        return

    logger.info("Sending WeCom notification at_mobiles=%s", at_mobiles or [])

    seen: set[str] = set()
    mentioned = [
        m for m in (at_mobiles or [])
        if m and not (m in seen or seen.add(m))  # type: ignore[func-returns-value]
    ]

    if mentioned:
        _post(webhook_url, {
            "msgtype": "text",
            "text": {
                "content": "请关注以下通知：",
                "mentioned_mobile_list": mentioned,
            },
        })

    _post(webhook_url, {
        "msgtype": "markdown",
        "markdown": {"content": content},
    })
