from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .logger import get_logger

logger = get_logger(__name__)

_VERDICT_RE = re.compile(
    r"\b(?P<role>product|developer):(?P<verdict>pass(?:ed)?|reject(?:ed)?)\b",
    re.IGNORECASE,
)
_CLOSES_RE = re.compile(r"[Cc]loses\s+#(\d+)")


class GitLabError(Exception):
    pass


class GitLabClient:
    def __init__(self, url: str, token: str) -> None:
        self._base = url.rstrip("/") + "/api/v4"
        self._token = token

    def _request(self, method: str, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method)
        req.add_header("PRIVATE-TOKEN", self._token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise GitLabError(f"HTTP {e.code} [{method} {path}]: {body_text}") from e
        except urllib.error.URLError as e:
            raise GitLabError(f"网络错误 [{method} {path}]: {e.reason}") from e

    def get_issue(self, project_id: int, issue_iid: int) -> Optional[dict]:
        try:
            return self._request("GET", f"/projects/{project_id}/issues/{issue_iid}")
        except GitLabError as e:
            logger.warning("get_issue #%s project=%s failed: %s", issue_iid, project_id, e)
            return None

    def get_issue_notes(self, project_id: int, issue_iid: int) -> list[dict]:
        notes: list[dict] = []
        page = 1
        try:
            while True:
                batch = self._request(
                    "GET",
                    f"/projects/{project_id}/issues/{issue_iid}/notes",
                    params={"per_page": 100, "page": page},
                )
                if not batch:
                    break
                notes.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        except GitLabError as e:
            logger.warning("get_issue_notes #%s project=%s failed: %s", issue_iid, project_id, e)
        return notes

    def get_mr_approvals(self, project_id: int, mr_iid: int) -> dict:
        try:
            return self._request("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/approvals")
        except GitLabError as e:
            logger.warning("get_mr_approvals !%s project=%s failed: %s", mr_iid, project_id, e)
            return {}

    def get_merged_mrs_to_pre(self, project_id: int) -> list[dict]:
        mrs: list[dict] = []
        page = 1
        try:
            while True:
                batch = self._request(
                    "GET",
                    f"/projects/{project_id}/merge_requests",
                    params={
                        "state": "merged",
                        "target_branch": "pre",
                        "per_page": 100,
                        "page": page,
                        "order_by": "merged_at",
                        "sort": "desc",
                    },
                )
                if not batch:
                    break
                mrs.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        except GitLabError as e:
            logger.warning("get_merged_mrs_to_pre project=%s failed: %s", project_id, e)
        return mrs

    def check_pre_verification_status(self, project_id: int) -> list[dict]:
        """查询 pre 上已合并 MR 关联的开放 Issue 的验收状态，返回未完成项。

        每项：{issue_iid, issue_title, issue_url, product_verdict, developer_verdict}
        verdict: 'pass' | 'reject' | 'pending'
        """
        mrs = self.get_merged_mrs_to_pre(project_id)
        issue_iids: set[int] = set()
        for mr in mrs:
            desc = mr.get("description") or ""
            for m in _CLOSES_RE.findall(desc):
                issue_iids.add(int(m))

        if not issue_iids:
            return []

        incomplete = []
        for iid in sorted(issue_iids):
            issue = self.get_issue(project_id, iid)
            if not issue or issue.get("state") != "opened":
                continue
            notes = self.get_issue_notes(project_id, iid)
            product_verdict = self._latest_verdict(notes, "product")
            developer_verdict = self._latest_verdict(notes, "developer")
            if product_verdict != "pass" or developer_verdict != "pass":
                incomplete.append({
                    "issue_iid": iid,
                    "issue_title": issue.get("title", ""),
                    "issue_url": issue.get("web_url", ""),
                    "product_verdict": product_verdict,
                    "developer_verdict": developer_verdict,
                })

        return incomplete

    @staticmethod
    def _latest_verdict(notes: list[dict], role: str) -> str:
        """返回最新的 pass/reject/pending，notes 按时间任意顺序均可。"""
        latest_time = ""
        latest_verdict = "pending"
        for note in notes:
            if note.get("system"):
                continue
            body = note.get("body", "").replace("：", ":")
            m = _VERDICT_RE.search(body)
            if m and m.group("role").lower() == role:
                t = note.get("created_at", "")
                if t > latest_time:
                    latest_time = t
                    raw = m.group("verdict").lower()
                    latest_verdict = "pass" if raw.startswith("pass") else "reject"
        return latest_verdict
