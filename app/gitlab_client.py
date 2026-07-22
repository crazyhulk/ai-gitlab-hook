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

    def _request(self, method: str, path: str, params: Optional[dict] = None, data: Optional[dict] = None) -> Any:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("PRIVATE-TOKEN", self._token)
        if body:
            req.add_header("Content-Type", "application/json")

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

    def get_mr(self, project_id: int, mr_iid: int) -> dict:
        try:
            return self._request("GET", f"/projects/{project_id}/merge_requests/{mr_iid}")
        except GitLabError as e:
            logger.warning("get_mr !%s project=%s failed: %s", mr_iid, project_id, e)
            return {}

    def get_mr_changes(self, project_id: int, mr_iid: int) -> list[dict]:
        """返回 MR 的完整文件变更列表。

        access_raw_diffs 避免大型 MR 因数据库 diff 限制遗漏受控目录文件。
        调用失败时向上抛错，由门禁逻辑按失败处理。
        """
        result = self._request(
            "GET",
            f"/projects/{project_id}/merge_requests/{mr_iid}/changes",
            params={"access_raw_diffs": "true"},
        )
        return result.get("changes") or []

    def compare_branches(self, project_id: int, from_branch: str, to_branch: str) -> dict:
        """比较两个分支，返回 to 相对 from 的 commits 和 diffs。

        默认 straight=false，即 three-dot 比较（git log from..to）。
        如果返回 commits 为空，说明 to 的所有 commit 都可从 from 到达。
        """
        try:
            return self._request(
                "GET",
                f"/projects/{project_id}/repository/compare",
                params={"from": from_branch, "to": to_branch},
            )
        except GitLabError as e:
            logger.warning(
                "compare_branches from=%s to=%s project=%s failed: %s",
                from_branch, to_branch, project_id, e,
            )
            return {}

    def get_merged_mrs_to_pre(self, project_id: int, pre_branch: str = "pre") -> list[dict]:
        mrs: list[dict] = []
        page = 1
        try:
            while True:
                batch = self._request(
                    "GET",
                    f"/projects/{project_id}/merge_requests",
                    params={
                        "state": "merged",
                        "target_branch": pre_branch,
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

    def check_pre_verification_status(self, project_id: int, pre_branch: str = "pre") -> list[dict]:
        """查询 pre 上已合并 MR 关联的开放 Issue 的验收状态，返回未完成项。

        每项：{issue_iid, issue_title, issue_url, product_verdict, developer_verdict}
        verdict: 'pass' | 'reject' | 'pending'
        """
        mrs = self.get_merged_mrs_to_pre(project_id, pre_branch)
        # 记录每个 issue 最晚的 merged_at，验收口令必须在此之后才算数
        issue_merged_at: dict[int, str] = {}
        for mr in mrs:
            desc = mr.get("description") or ""
            merged_at: str = mr.get("merged_at") or ""
            for m in _CLOSES_RE.findall(desc):
                iid = int(m)
                if iid not in issue_merged_at or merged_at > issue_merged_at[iid]:
                    issue_merged_at[iid] = merged_at

        if not issue_merged_at:
            return []

        incomplete = []
        for iid in sorted(issue_merged_at):
            issue = self.get_issue(project_id, iid)
            if not issue or issue.get("state") != "opened":
                continue
            notes = self.get_issue_notes(project_id, iid)
            since = issue_merged_at[iid]
            product_verdict = self._latest_verdict(notes, "product", since)
            developer_verdict = self._latest_verdict(notes, "developer", since)
            if product_verdict != "pass" or developer_verdict != "pass":
                incomplete.append({
                    "issue_iid": iid,
                    "issue_title": issue.get("title", ""),
                    "issue_url": issue.get("web_url", ""),
                    "product_verdict": product_verdict,
                    "developer_verdict": developer_verdict,
                })

        return incomplete

    def set_commit_status(
        self,
        project_id: int,
        commit_sha: str,
        state: str,
        name: str = "acceptance-check",
        description: str = "",
        target_url: str = "",
    ) -> bool:
        """设置 commit 的状态，用于 MR 合并门禁。

        Args:
            project_id: 项目 ID
            commit_sha: commit SHA
            state: 状态，可选值：pending, running, success, failed, canceled
            name: 状态名称，会显示在 GitLab MR 页面
            description: 状态描述
            target_url: 点击状态时跳转的 URL

        Returns:
            成功返回 True，失败返回 False
        """
        data = {
            "state": state,
            "name": name,
        }
        if description:
            data["description"] = description
        if target_url:
            data["target_url"] = target_url

        try:
            self._request(
                "POST",
                f"/projects/{project_id}/statuses/{commit_sha}",
                data=data,
            )
            logger.info(
                "Set commit status project=%s sha=%s state=%s name=%s",
                project_id, commit_sha[:8], state, name,
            )
            return True
        except GitLabError as e:
            logger.error(
                "Failed to set commit status project=%s sha=%s state=%s: %s",
                project_id, commit_sha[:8], state, e,
            )
            return False

    @staticmethod
    def _latest_verdict(notes: list[dict], role: str, since_time: str = "") -> str:
        """返回最新的 pass/reject/pending，notes 按时间任意顺序均可。

        since_time: 只统计 created_at > since_time 的评论（ISO8601 字符串比较）。
        """
        latest_time = ""
        latest_verdict = "pending"
        for note in notes:
            if note.get("system"):
                continue
            body = note.get("body", "").replace("：", ":")
            m = _VERDICT_RE.search(body)
            if m and m.group("role").lower() == role:
                t = note.get("created_at", "")
                if since_time and t <= since_time:
                    continue
                if t > latest_time:
                    latest_time = t
                    raw = m.group("verdict").lower()
                    latest_verdict = "pass" if raw.startswith("pass") else "reject"
        return latest_verdict
