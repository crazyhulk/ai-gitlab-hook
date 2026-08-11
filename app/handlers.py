from __future__ import annotations

import re
from typing import Any

from . import state
from .config import Config
from .logger import get_logger
from .wechat import send_webhook

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────
# Issue 类型识别（与 ai-workflow/cc/validator.py 保持一致）
# 已去除 ## 小节格式校验，走轻量流程，仅要求指派研发负责人
# ──────────────────────────────────────────────────────────────


def _detect_issue_type(description: str) -> str | None:
    """根据 description 中的唯一标识章节推断 issue 类型。"""
    body = description or ""
    if re.search(r"##\s*优化背景", body):
        return "improve"
    if re.search(r"##\s*需求背景", body):
        return "feature"
    if re.search(r"##\s*问题现象", body):
        return "bug"
    if re.search(r"##\s*变更说明", body):
        return "quickfix"
    return None


def _is_feature_branch(branch: str) -> bool:
    """判断是否为功能分支（新格式 feature/ 或旧格式 issue_）"""
    return branch.startswith("feature/") or branch.startswith("issue_")


def _is_hotfix_branch(branch: str) -> bool:
    """判断是否为热修分支（新格式 hotfix/ 或旧格式 hotfix_）"""
    return branch.startswith("hotfix/") or branch.startswith("hotfix_")


def _is_quickfix_branch(branch: str) -> bool:
    """判断是否为快速迭代分支"""
    return branch.startswith("quickfix/")


def _is_feature_or_hotfix_branch(branch: str) -> bool:
    """判断是否为功能、热修或快速迭代分支"""
    return _is_feature_branch(branch) or _is_hotfix_branch(branch) or _is_quickfix_branch(branch)


# ──────────────────────────────────────────────────────────────
# Issue Hook
# 覆盖的人工通知节点：
#   节点1 — 新需求 Issue 创建后，产品通知研发认领
#   节点2 — Issue 指派研发后，产品通知研发认领
#   节点6 — 线上 Bug Issue 创建后，产品通知研发+TL 热修
# ──────────────────────────────────────────────────────────────

def handle_issue_event(payload: dict[str, Any], config: Config) -> str:
    attrs = payload.get("object_attributes", {}) or {}
    action = attrs.get("action", "")
    changes = payload.get("changes") or {}
    if not action:
        issue_state = attrs.get("state", "")
        if issue_state == "opened":
            action = "update" if changes else "open"
        elif issue_state in ("closed", "reopened"):
            action = issue_state
    issue_iid = attrs.get("iid")
    issue_title = attrs.get("title", "")
    issue_url = attrs.get("url", "")
    description = attrs.get("description", "") or ""

    project = payload.get("project", {}) or {}
    project_name = project.get("name", "")

    user = payload.get("user", {}) or {}
    reporter_name = user.get("name", "")
    reporter_username = user.get("username", "")

    assignees: list[dict] = payload.get("assignees") or []
    assignee_names = [a.get("name", "") for a in assignees if a.get("name")]
    assignee_usernames = [a.get("username", "") for a in assignees if a.get("username")]

    issue_type = _detect_issue_type(description)
    is_feature = issue_type in ("feature", "improve", "quickfix")
    is_bug = issue_type == "bug"

    logger.info(
        "Issue event action=%s issue=#%s type=%s title=%r project=%s reporter=%s assignees=%s",
        action, issue_iid, issue_type, issue_title, project_name, reporter_username, assignee_usernames,
    )

    if action in ("open", "reopen"):
        if is_feature:
            return _on_feature_issue_open(
                config, issue_iid, issue_title, issue_url, description,
                reporter_name, reporter_username, assignee_names, assignee_usernames,
                issue_type=issue_type,
            )
        if is_bug:
            return _on_bug_issue_open(
                config, issue_iid, issue_title, issue_url, description,
                reporter_name, reporter_username, assignee_names, assignee_usernames,
            )
        logger.info("Issue #%s type not detected from description, skipping", issue_iid)
        return "ignored"

    if action == "update":
        if is_feature or is_bug:
            notified = False
            if "assignees" in changes:
                assignees_change = changes["assignees"]
                new_assignees = _diff_assignees(assignees_change)
                if new_assignees:
                    _on_issue_assignee_change(
                        config, issue_iid, issue_title, issue_url,
                        reporter_name, reporter_username, description,
                        new_assignees, is_bug=is_bug, issue_type=issue_type,
                    )
                    notified = True
                elif not (assignees_change.get("current") or []) and (assignees_change.get("previous") or []):
                    type_label = {"bug": "Bug", "improve": "优化", "quickfix": "快速迭代"}.get(issue_type or "", "需求")
                    content = (
                        f"### ⚠️ Issue 负责人被全部移除\n"
                        f"> **Issue #{issue_iid}**：{issue_title}\n"
                        f"> **类型**：{type_label}\n"
                        f"> **操作人**：{reporter_name}\n"
                        f"> [查看 Issue]({issue_url})\n\n"
                        f"**注意**：此 Issue 当前无人负责，请重新指派研发。"
                    )
                    at_mobiles = config.tl_mobiles
                    logger.warning("Issue #%s all assignees removed type=%s project=%s", issue_iid, issue_type, project_name)
                    state.record_violation(
                        operator=reporter_username,
                        operator_name=reporter_name,
                        violation_type="issue_assignee_all_removed",
                        description=f"Issue #{issue_iid}「{issue_title}」负责人被全部移除",
                        project=project_name,
                        detail={"issue_iid": issue_iid, "issue_type": issue_type, "issue_url": issue_url},
                    )
                    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
                    notified = True
            # 已去除 ## 小节格式校验，描述变更不再触发"格式补全后认领"通知
            return "ok" if notified else "ignored"
        return "ignored"

    if action == "close":
        notified = False
        if not assignees and issue_type in ("feature", "improve", "bug", "quickfix"):
            type_label = {"bug": "Bug", "improve": "优化", "quickfix": "快速迭代"}.get(issue_type or "", "需求")
            content = (
                f"### ⚠️ Issue 无人认领即关闭\n"
                f"> **Issue #{issue_iid}**：{issue_title}\n"
                f"> **类型**：{type_label}\n"
                f"> **关闭人**：{reporter_name}\n"
                f"> [查看 Issue]({issue_url})\n\n"
                f"**注意**：此 Issue 未指派给任何人就被关闭，请确认是否遗漏处理。"
            )
            at_mobiles = config.tl_mobiles
            logger.warning("Issue #%s closed with no assignee type=%s project=%s", issue_iid, issue_type, project_name)
            state.record_violation(
                operator=reporter_username,
                operator_name=reporter_name,
                violation_type="issue_closed_no_assignee",
                description=f"Issue #{issue_iid}「{issue_title}」无人认领即关闭",
                project=project_name,
                detail={"issue_iid": issue_iid, "issue_type": issue_type, "issue_url": issue_url},
            )
            send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
            notified = True

        if issue_type in ("feature", "improve", "quickfix") and assignees and config.gitlab_client:
            project_id = (payload.get("project") or {}).get("id")
            if project_id:
                notes = config.gitlab_client.get_issue_notes(project_id, issue_iid)
                product_verdict = config.gitlab_client._latest_verdict(notes, "product")
                developer_verdict = config.gitlab_client._latest_verdict(notes, "developer")
                if product_verdict != "pass" or developer_verdict != "pass":
                    type_label = {"improve": "优化", "quickfix": "快速迭代"}.get(issue_type, "需求")
                    _vl = {"pass": "已通过", "reject": "已拒绝", "pending": "未验收"}
                    pending_parts = []
                    if product_verdict != "pass":
                        pending_parts.append(f"产品：{_vl[product_verdict]}")
                    if developer_verdict != "pass":
                        pending_parts.append(f"研发：{_vl[developer_verdict]}")
                    verdict_label = "、".join(pending_parts)
                    content = (
                        f"### ⚠️ {type_label} Issue 未完成双方验收即关闭\n"
                        f"> **Issue #{issue_iid}**：{issue_title}\n"
                        f"> **关闭人**：{reporter_name}\n"
                        f"> **验收状态**：{verdict_label}\n"
                        f"> [查看 Issue]({issue_url})\n\n"
                        f"**注意**：该 {type_label} Issue 需 `product:pass` 和 `developer:pass` 双方确认后才可关闭，"
                        f"请补充验收口令后重新关闭。"
                    )
                    at_mobiles = list(dict.fromkeys(
                        config.tl_mobiles + config.resolve_wechat_ids(assignee_usernames + [reporter_username])
                    ))
                    logger.warning(
                        "Issue #%s closed without full acceptance type=%s product=%s developer=%s project=%s",
                        issue_iid, issue_type, product_verdict, developer_verdict, project_name,
                    )
                    state.record_violation(
                        operator=reporter_username,
                        operator_name=reporter_name,
                        violation_type="issue_closed_no_product_pass",
                        description=f"Issue #{issue_iid}「{issue_title}」未完成双方验收即关闭（{verdict_label}）",
                        project=project_name,
                        detail={"issue_iid": issue_iid, "issue_type": issue_type,
                                "product_verdict": product_verdict, "developer_verdict": developer_verdict,
                                "issue_url": issue_url},
                    )
                    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
                    notified = True

        return "ok" if notified else "ignored"

    logger.info("Issue event action=%s not handled", action)
    return "ignored"


def _on_feature_issue_open(
    config: Config,
    issue_iid, issue_title, issue_url, description,
    reporter_name, reporter_username, assignee_names, assignee_usernames,
    issue_type: str = "feature",
) -> str:
    type_label = {"improve": "优化", "quickfix": "快速迭代"}.get(issue_type, "需求")
    # 走轻量流程，不校验 ## 小节格式，仅要求指派了研发负责人
    if not assignee_usernames:
        content = (
            f"### {type_label} Issue 待指派研发\n"
            f"> **Issue #{issue_iid}**：{issue_title}\n"
            f"> **提出人**：{reporter_name}\n"
            f"> [查看 Issue]({issue_url})\n\n"
            f"**下一步 · {reporter_name}（产品）**\n"
            f"> 请指派研发负责人"
        )
        at_mobiles = config.resolve_wechat_ids([reporter_username])
        logger.info("Feature/improve issue #%s no assignee, notifying reporter=%s", issue_iid, reporter_username)
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        return "ok"

    # 格式合规 → 通知研发认领
    assignee_str = "、".join(assignee_names) if assignee_names else "（待指派）"
    if issue_type == "quickfix":
        heading = "### ⚡ 快速迭代 Issue 待认领"
        cmd = f"ccg gitlab quickfix start {issue_iid}"
        merge_hint = "\n> ⚠️ 快速迭代分支直接合入 `main`，无需走 pre 验收"
    else:
        heading = f"### 新{type_label} Issue 待认领"
        cmd = f"ccg gitlab feature start {issue_iid}"
        merge_hint = ""
    content = (
        f"{heading}\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **提出人**：{reporter_name}\n"
        f"> **负责人**：{assignee_str}\n"
        f"> [查看 Issue]({issue_url})\n\n"
        f"**下一步 · {assignee_str or '研发'}**\n"
        f"> 请阅读{type_label}后执行以下命令认领并拉取分支：\n"
        f"> `{cmd}`"
        f"{merge_hint}"
    )
    at_mobiles = config.resolve_wechat_ids(assignee_usernames)
    logger.info("Feature/improve/quickfix issue #%s created type=%s, notifying assignees=%s", issue_iid, issue_type, assignee_usernames)
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


def _on_bug_issue_open(
    config: Config,
    issue_iid, issue_title, issue_url, description,
    reporter_name, reporter_username, assignee_names, assignee_usernames,
) -> str:
    # 走轻量流程，不校验 ## 小节格式，仅要求指派了研发负责人
    if not assignee_usernames:
        content = (
            f"### Bug Issue 待指派研发\n"
            f"> **Issue #{issue_iid}**：{issue_title}\n"
            f"> **提出人**：{reporter_name}\n"
            f"> [查看 Issue]({issue_url})\n\n"
            f"**下一步 · {reporter_name}（产品）**\n"
            f"> 请指派研发负责人"
        )
        at_mobiles = config.resolve_wechat_ids([reporter_username])
        logger.info("Bug issue #%s no assignee, notifying reporter=%s", issue_iid, reporter_username)
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        return "ok"

    # 格式合规 → 通知研发 + TL（线上 Bug 紧急）
    assignee_str = "、".join(assignee_names) if assignee_names else "（待指派）"
    tl_str = "、".join(config.tl_usernames) if config.tl_usernames else "TL"
    content = (
        f"### 🚨 线上 Bug 待处理\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **提出人**：{reporter_name}\n"
        f"> **负责人**：{assignee_str}\n"
        f"> [查看 Issue]({issue_url})\n\n"
        f"**下一步 · {assignee_str or '研发'}**\n"
        f"> 线上 Bug 紧急，请优先处理！执行以下命令拉取热修分支：\n"
        f"> `ccg gitlab hotfix start {issue_iid}`"
    )
    at_mobiles = config.resolve_wechat_ids(assignee_usernames) + config.tl_mobiles
    logger.info("Bug issue #%s created, notifying assignees=%s tl=%s", issue_iid, assignee_usernames, config.tl_usernames)
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


def _diff_assignees(assignees_change: dict) -> list[dict]:
    """返回新增的 assignee 列表（current 中有、previous 中没有的）。"""
    prev_ids = {a.get("id") for a in (assignees_change.get("previous") or [])}
    return [a for a in (assignees_change.get("current") or []) if a.get("id") not in prev_ids]


def _on_issue_assignee_change(
    config: Config,
    issue_iid, issue_title, issue_url,
    reporter_name: str,
    reporter_username: str,
    description: str,
    new_assignees: list[dict],
    is_bug: bool,
    issue_type: str | None,
) -> None:
    type_label = {"bug": "Bug", "improve": "优化", "quickfix": "快速迭代"}.get(issue_type or "", "需求")
    assignee_names = [a.get("name", "") for a in new_assignees if a.get("name")]
    assignee_usernames = [a.get("username", "") for a in new_assignees if a.get("username")]
    assignee_str = "、".join(assignee_names)

    # 走轻量流程，不再校验 ## 小节格式；指派即通知研发认领
    if is_bug:
        cmd = f"ccg gitlab hotfix start {issue_iid}"
    elif issue_type == "quickfix":
        cmd = f"ccg gitlab quickfix start {issue_iid}"
    else:
        cmd = f"ccg gitlab feature start {issue_iid}"
    merge_hint = "\n> ⚠️ 快速迭代分支直接合入 `main`，无需走 pre 验收" if issue_type == "quickfix" else ""
    content = (
        f"### {type_label} Issue 已指派\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **提出人**：{reporter_name}\n"
        f"> **负责人**：{assignee_str}\n"
        f"> [查看 Issue]({issue_url})\n\n"
        f"**下一步 · {assignee_str}**\n"
        f"> 请阅读 Issue 后执行以下命令认领并拉取分支：\n"
        f"> `{cmd}`"
        f"{merge_hint}"
    )
    at_mobiles = config.resolve_wechat_ids(assignee_usernames)
    logger.info("Issue #%s assignee changed type=%s, notifying new assignees=%s", issue_iid, issue_type, assignee_usernames)
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)


# ──────────────────────────────────────────────────────────────
# Merge Request Hook
# 覆盖的人工通知节点：
#   节点3 — 需求 MR Approve 后，Reviewer 通知研发可合并
#   节点4 — 上线 MR Approve 后，TL 通知研发可上线
#   节点5 — 热修 MR Approve 后，Reviewer 通知研发可合并
# 违规检测：
#   - 功能分支（issue_*）直接合入 main/master，绕过 pre
#   - 上线 MR 创建时 pre 双向验收未完成
# ──────────────────────────────────────────────────────────────

_CLOSES_RE = re.compile(r"[Cc]loses\s+#\d+")


def _update_frontend_review_status(
    config: Config,
    payload: dict[str, Any],
    attrs: dict[str, Any],
) -> bool:
    """更新 frontend-v1 目录的指定 Reviewer 审批门禁。"""
    if not config.gitlab_client:
        return False

    project = payload.get("project", {}) or {}
    project_id = project.get("id")
    mr_iid = attrs.get("iid")
    mr_url = attrs.get("url", "")
    commit_sha = attrs.get("last_commit", {}).get("id") if isinstance(attrs.get("last_commit"), dict) else None
    if not commit_sha:
        commit_sha = attrs.get("sha")

    if not project_id or not mr_iid or not commit_sha:
        logger.warning(
            "Skipping frontend review check: project_id=%s mr_iid=%s commit_sha=%s",
            project_id, mr_iid, bool(commit_sha),
        )
        return False

    review_path = config.frontend_review_path
    required_reviewers = set(config.frontend_required_reviewers)

    try:
        changes = config.gitlab_client.get_mr_changes(project_id, mr_iid)
        touches_frontend = bool(review_path) and any(
            (change.get("old_path") or "").startswith(review_path)
            or (change.get("new_path") or "").startswith(review_path)
            for change in changes
        )

        if not touches_frontend:
            config.gitlab_client.set_commit_status(
                project_id=project_id,
                commit_sha=commit_sha,
                state="success",
                name="frontend-review-check",
                description=f"未修改 {review_path}，无需指定前端 Reviewer",
            )
            return True

        approvals = config.gitlab_client.get_mr_approvals(project_id, mr_iid)
        approved_usernames = {
            item.get("user", {}).get("username", "")
            for item in (approvals.get("approved_by") or [])
        }
        matched_reviewers = sorted(required_reviewers & approved_usernames)

        if matched_reviewers:
            description = f"{review_path} 已由 {', '.join(matched_reviewers)} Approve"
            state_name = "success"
        else:
            reviewers_label = " 或 ".join(sorted(required_reviewers))
            description = f"修改了 {review_path}，需 {reviewers_label} Approve"
            state_name = "failed"

        config.gitlab_client.set_commit_status(
            project_id=project_id,
            commit_sha=commit_sha,
            state=state_name,
            name="frontend-review-check",
            description=description[:255],
            target_url=mr_url,
        )
        logger.info(
            "Updated frontend review check MR !%s state=%s matched_reviewers=%s",
            mr_iid, state_name, matched_reviewers,
        )
        return True
    except Exception as e:
        logger.warning("Frontend review check failed for MR !%s: %s", mr_iid, e)
        config.gitlab_client.set_commit_status(
            project_id=project_id,
            commit_sha=commit_sha,
            state="failed",
            name="frontend-review-check",
            description="前端 Reviewer 检查执行失败，请联系管理员",
            target_url=mr_url,
        )
        return True


def _on_mr_open(
    payload: dict[str, Any],
    attrs: dict[str, Any],
    config: Config,
) -> str:
    mr_iid = attrs.get("iid", "?")
    mr_title = attrs.get("title", "")
    mr_url = attrs.get("url", "")
    source_branch: str = attrs.get("source_branch", "")
    target_branch: str = attrs.get("target_branch", "")

    user = payload.get("user", {}) or {}
    author_name = user.get("name", "")
    author_username = user.get("username", "")

    project = payload.get("project", {}) or {}
    project_name = project.get("name", "")
    project_id = project.get("id")

    violations_found = False
    frontend_checked = _update_frontend_review_status(config, payload, attrs)

    # 热修 MR（hotfix_* → main），检查 Approve 数量
    is_hotfix_to_main = _is_hotfix_branch(source_branch) and target_branch == config.main_branch
    if is_hotfix_to_main:
        if config.gitlab_client is None:
            logger.warning("GitLab client not configured, skipping approval check for MR !%s", mr_iid)
        else:
            try:
                approvals_data = config.gitlab_client.get_mr_approvals(project_id, mr_iid)
                approved_by = approvals_data.get("approved_by") or []
                current_approvals = len(approved_by)
                required_approvals = config.hotfix_required_approvals
            except Exception as e:
                logger.warning("Approval check failed for MR !%s: %s", mr_iid, e)
                current_approvals = 0
                required_approvals = config.hotfix_required_approvals

            # 设置 Commit Status 作为合并门禁
            commit_sha = attrs.get("last_commit", {}).get("id") if isinstance(attrs.get("last_commit"), dict) else None
            if not commit_sha:
                commit_sha = attrs.get("sha")

            if commit_sha and project_id:
                if current_approvals < required_approvals:
                    # Approve 不足 → 设置为 failed，阻止合并
                    description = f"需要 {required_approvals} 人 Approve，当前 {current_approvals} 人"
                    config.gitlab_client.set_commit_status(
                        project_id=project_id,
                        commit_sha=commit_sha,
                        state="failed",
                        name="hotfix-approval-check",
                        description=description,
                        target_url=mr_url,
                    )
                    logger.info("Set commit status failed for hotfix MR !%s: %s/%s approvals", mr_iid, current_approvals, required_approvals)
                else:
                    # Approve 已满足 → 设置为 success，允许合并
                    description = f"✓ 已获得 {current_approvals} 人 Approve，满足要求"
                    config.gitlab_client.set_commit_status(
                        project_id=project_id,
                        commit_sha=commit_sha,
                        state="success",
                        name="hotfix-approval-check",
                        description=description,
                    )
                    logger.info("Set commit status success for hotfix MR !%s: %s approvals", mr_iid, current_approvals)

    # 上线 MR（pre → main），检查 pre 双向验收是否完成
    is_release_mr = source_branch == config.pre_branch and target_branch == config.main_branch
    if is_release_mr:
        if config.gitlab_client is None:
            logger.warning("GitLab client not configured, skipping pre verification check for MR !%s", mr_iid)
        else:
            try:
                incomplete = config.gitlab_client.check_pre_verification_status(project_id, config.pre_branch)
            except Exception as e:
                logger.warning("Pre verification check failed for MR !%s: %s", mr_iid, e)
                incomplete = []

            # 设置 Commit Status 作为合并门禁
            commit_sha = attrs.get("last_commit", {}).get("id") if isinstance(attrs.get("last_commit"), dict) else None
            if not commit_sha:
                # 兼容不同 GitLab 版本的 payload 格式
                commit_sha = attrs.get("sha")

            if commit_sha and project_id:
                if incomplete:
                    # 验收未完成 → 设置为 failed，阻止合并
                    issue_ids = [item["issue_iid"] for item in incomplete]
                    # 构建详细的状态描述（GitLab 限制 255 字符，简洁版）
                    status_parts = []
                    for item in incomplete[:3]:  # 最多显示 3 个
                        iid = item["issue_iid"]
                        p = "✓" if item["product_verdict"] == "pass" else "✗"
                        d = "✓" if item["developer_verdict"] == "pass" else "✗"
                        status_parts.append(f"#{iid}(产品{p} 研发{d})")
                    if len(incomplete) > 3:
                        status_parts.append(f"等{len(incomplete)}个")
                    description = f"验收未完成: {' '.join(status_parts)}"

                    config.gitlab_client.set_commit_status(
                        project_id=project_id,
                        commit_sha=commit_sha,
                        state="failed",
                        name="pre-acceptance-check",
                        description=description[:255],  # GitLab 限制 255 字符
                        target_url=incomplete[0]["issue_url"] if incomplete else "",  # 点击跳转到第一个 Issue
                    )
                else:
                    # 验收已完成 → 设置为 success，允许合并
                    config.gitlab_client.set_commit_status(
                        project_id=project_id,
                        commit_sha=commit_sha,
                        state="success",
                        name="pre-acceptance-check",
                        description="✓ 所有 Issue 验收已完成，可以合并",
                    )

            if incomplete:
                _verdict_label = {"pass": "✓ 已通过", "reject": "✗ 已拒绝", "pending": "✗ 未验收"}
                issue_lines = "\n".join(
                    f"> **Issue #{item['issue_iid']}**：[{item['issue_title']}]({item['issue_url']})\n"
                    f">   产品：{_verdict_label[item['product_verdict']]}　　"
                    f"研发：{_verdict_label[item['developer_verdict']]}"
                    for item in incomplete
                )
                issue_ids = [item["issue_iid"] for item in incomplete]

                # 构建详细的操作指引，包含评论区直达链接
                pending_actions = []
                for item in incomplete:
                    actions = []
                    if item['product_verdict'] != 'pass':
                        actions.append("产品需评论：`product:pass`")
                    if item['developer_verdict'] != 'pass':
                        actions.append("研发需评论：`developer:pass`")
                    if actions:
                        # Issue 评论区链接：在 Issue URL 后加 #note_xxx 或直接跳转
                        comment_url = f"{item['issue_url']}#notes"
                        pending_actions.append(
                            f"> **Issue #{item['issue_iid']}**：{' 且 '.join(actions)}\n"
                            f"> [点击进入评论区]({comment_url})"
                        )

                action_guide = "\n".join(pending_actions)

                content = (
                    f"### ⚠️ 上线 MR 创建，但 pre 验收未完成\n"
                    f"> **MR !{mr_iid}**：{mr_title}\n"
                    f"> **操作人**：{author_name}（`{author_username}`）\n"
                    f"> [查看 MR]({mr_url})\n\n"
                    f"**验收状态**\n"
                    f"{issue_lines}\n\n"
                    f"**下一步操作**\n"
                    f"{action_guide}\n\n"
                    f"**说明**\n"
                    f"> - 验收完成前，GitLab 页面的 Merge 按钮将被禁用\n"
                    f"> - 发布验收口令后，系统会自动更新 MR 状态\n"
                    f"> - 双方都通过后，Merge 按钮自动解锁"
                )
                at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
                logger.warning("Release MR !%s created but incomplete pre verifications: %s", mr_iid, issue_ids)
                state.record_violation(
                    operator=author_username, operator_name=author_name,
                    violation_type="mr_release_unverified",
                    description=f"MR !{mr_iid}「{mr_title}」上线前 pre 验收未完成，涉及 Issue: {issue_ids}",
                    project=project_name,
                    detail={"mr_iid": mr_iid, "incomplete_issues": issue_ids, "mr_url": mr_url},
                )
                send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
                violations_found = True

    # 功能分支直接合入 main，跳过 pre
    if _is_feature_branch(source_branch) and target_branch == config.main_branch:
        content = (
            f"### ⚠️ 功能分支直接合入 {target_branch}，违反上线流程\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **操作人**：{author_name}（`{author_username}`）\n"
            f"> `{source_branch}` → `{target_branch}`\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**注意**：功能分支应先合入 `{config.pre_branch}` 完成验收，再通过上线 MR 合入 `{target_branch}`。"
        )
        at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
        logger.warning("MR !%s feature branch direct to %s author=%s project=%s", mr_iid, target_branch, author_username, project_name)
        state.record_violation(
            operator=author_username, operator_name=author_name,
            violation_type="mr_feature_to_main",
            description=f"MR !{mr_iid}「{mr_title}」功能分支 {source_branch} 直接合入 {target_branch}",
            project=project_name,
            detail={"mr_iid": mr_iid, "source_branch": source_branch, "target_branch": target_branch, "mr_url": mr_url},
        )
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        violations_found = True

    # feature/hotfix MR 缺少 Closes #xxx（未通过 ccg mr create 创建）
    if _is_feature_or_hotfix_branch(source_branch):
        mr_description = attrs.get("description") or ""
        if not _CLOSES_RE.search(mr_description):
            # 设置 Commit Status 阻止合并（对 pre 和 main 都生效，防止绕过验收）
            is_to_pre = target_branch == config.pre_branch
            is_to_main = target_branch in (config.main_branch, "master")

            if (is_to_pre or is_to_main) and config.gitlab_client and project_id:
                commit_sha = attrs.get("last_commit", {}).get("id") if isinstance(attrs.get("last_commit"), dict) else None
                if not commit_sha:
                    commit_sha = attrs.get("sha")

                if commit_sha:
                    config.gitlab_client.set_commit_status(
                        project_id=project_id,
                        commit_sha=commit_sha,
                        state="failed",
                        name="issue-reference-check",
                        description="MR 描述缺少 Closes #xxx 引用",
                        target_url=mr_url,
                    )
                    logger.info("Set commit status failed for MR !%s: missing Closes reference", mr_iid)

            content = (
                f"### ⚠️ MR 未关联 Issue（缺少 Closes #xxx）\n"
                f"> **MR !{mr_iid}**：{mr_title}\n"
                f"> **操作人**：{author_name}（`{author_username}`）\n"
                f"> `{source_branch}` → `{target_branch}`\n"
                f"> [查看 MR]({mr_url})\n\n"
                f"**问题**\n"
                f"> MR 描述中未找到 `Closes #xxx` 引用，可能未通过 `ccg gitlab mr create` 创建\n\n"
                f"**影响**\n"
                f"> - Issue 无法自动关闭\n"
                f"> - 上线时无法追踪验收状态\n"
                + (f"> - **GitLab 页面 Merge 按钮已被禁用**\n\n" if (is_to_pre or is_to_main) else "\n")
                + f"**下一步 · {author_name}**\n"
                f"> 请编辑 MR 描述，添加 `Closes #<issue_id>` 引用"
                + (f"\n> 添加后系统会自动解除合并限制" if (is_to_pre or is_to_main) else "")
            )
            at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
            logger.warning("MR !%s missing Closes ref source=%s target=%s author=%s project=%s", mr_iid, source_branch, target_branch, author_username, project_name)
            state.record_violation(
                operator=author_username, operator_name=author_name,
                violation_type="mr_missing_closes_ref",
                description=f"MR !{mr_iid}「{mr_title}」描述缺少 Closes #xxx，可能未通过 ccg 创建",
                project=project_name,
                detail={"mr_iid": mr_iid, "source_branch": source_branch, "target_branch": target_branch, "mr_url": mr_url},
            )
            send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
            violations_found = True

        if _is_quickfix_branch(source_branch):
            expected_prefix = "[快速迭代]"
        elif _is_feature_branch(source_branch):
            expected_prefix = "[需求]"
        else:
            expected_prefix = "[Bug热修]"
        if not mr_title.startswith(expected_prefix):
            branch_label = "快速迭代" if _is_quickfix_branch(source_branch) else ("需求" if _is_feature_branch(source_branch) else "热修")
            content = (
                f"### ⚠️ MR 标题不符规范（可能未通过 ccg 创建）\n"
                f"> **MR !{mr_iid}**：{mr_title}\n"
                f"> **操作人**：{author_name}（`{author_username}`）\n"
                f"> `{source_branch}` → `{target_branch}`\n"
                f"> [查看 MR]({mr_url})\n\n"
                f"**注意**：{branch_label} MR 标题应以 `{expected_prefix}` 开头，"
                f"请确认是否通过 `ccg gitlab mr create` 创建。"
            )
            at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
            logger.warning("MR !%s title format mismatch title=%r source=%s author=%s project=%s", mr_iid, mr_title, source_branch, author_username, project_name)
            state.record_violation(
                operator=author_username, operator_name=author_name,
                violation_type="mr_title_format",
                description=f"MR !{mr_iid}「{mr_title}」标题不符 ccg 规范（应以 {expected_prefix} 开头）",
                project=project_name,
                detail={"mr_iid": mr_iid, "mr_title": mr_title, "expected_prefix": expected_prefix, "mr_url": mr_url},
            )
            send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
            violations_found = True

    return "ok" if violations_found or frontend_checked else "ignored"


def _on_mr_update(
    payload: dict[str, Any],
    attrs: dict[str, Any],
    config: Config,
) -> str:
    """处理 MR 更新事件，刷新 diff 门禁并检查 Closes #xxx。"""
    frontend_checked = _update_frontend_review_status(config, payload, attrs)
    changes = payload.get("changes", {})
    if "description" not in changes:
        return "ok" if frontend_checked else "ignored"

    mr_iid = attrs.get("iid", "?")
    source_branch: str = attrs.get("source_branch", "")
    target_branch: str = attrs.get("target_branch", "")

    # 只处理 feature/hotfix → pre 的 MR
    if not _is_feature_or_hotfix_branch(source_branch):
        return "ignored"
    if target_branch != config.pre_branch:
        return "ignored"

    project = payload.get("project", {}) or {}
    project_id = project.get("id")

    if not config.gitlab_client or not project_id:
        return "ignored"

    mr_description = attrs.get("description") or ""
    mr_url = attrs.get("url", "")

    # 检查更新后的 description 是否包含 Closes #xxx
    has_closes = _CLOSES_RE.search(mr_description)

    commit_sha = attrs.get("last_commit", {}).get("id") if isinstance(attrs.get("last_commit"), dict) else None
    if not commit_sha:
        commit_sha = attrs.get("sha")

    if not commit_sha:
        return "ignored"

    if has_closes:
        # 补充了 Closes #xxx，更新状态为 success
        config.gitlab_client.set_commit_status(
            project_id=project_id,
            commit_sha=commit_sha,
            state="success",
            name="issue-reference-check",
            description="✓ MR 已关联 Issue",
        )
        logger.info("MR !%s description updated with Closes reference, status set to success", mr_iid)
    else:
        # 仍然缺少 Closes #xxx，保持 failed
        config.gitlab_client.set_commit_status(
            project_id=project_id,
            commit_sha=commit_sha,
            state="failed",
            name="issue-reference-check",
            description="MR 描述缺少 Closes #xxx 引用",
            target_url=mr_url,
        )
        logger.info("MR !%s description updated but still missing Closes reference", mr_iid)

    return "ok"


def _on_mr_merged(
    payload: dict[str, Any],
    attrs: dict[str, Any],
    config: Config,
) -> str:
    source_branch: str = attrs.get("source_branch", "")
    target_branch: str = attrs.get("target_branch", "")

    project = payload.get("project", {}) or {}
    project_id = project.get("id")
    project_name = project.get("name", "")

    if not _is_feature_or_hotfix_branch(source_branch):
        return "ignored"

    if config.gitlab_client is None:
        logger.warning("GitLab client not configured, skipping merge approval check")
        return "ignored"

    mr_iid = attrs.get("iid", "?")
    mr_title = attrs.get("title", "")
    mr_url = attrs.get("url", "")

    user = payload.get("user", {}) or {}
    author_name = user.get("name", "")
    author_username = user.get("username", "")

    is_hotfix_to_main = _is_hotfix_branch(source_branch) and target_branch == config.main_branch
    required = config.hotfix_required_approvals if is_hotfix_to_main else 1

    approvals = config.gitlab_client.get_mr_approvals(project_id, mr_iid)
    approved_count = len(approvals.get("approved_by") or [])

    if approved_count < required:
        type_label = "热修" if _is_hotfix_branch(source_branch) else ("快速迭代" if _is_quickfix_branch(source_branch) else "需求")
        content = (
            f"### ⚠️ {type_label} MR 审批不足即合并（绕过 ccg 门禁）\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **操作人**：{author_name}（`{author_username}`）\n"
            f"> `{source_branch}` → `{target_branch}`\n"
            f"> **审批状态**：{approved_count}/{required} 人 Approve\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**注意**：{type_label} MR 需要 {required} 人 Approve 才允许合并，"
            f"此次合并可能直接在 GitLab 页面操作，绕过了 `ccg gitlab mr merge` 门禁检查。"
        )
        at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
        logger.warning(
            "MR !%s merged without sufficient approval %s/%s author=%s project=%s",
            mr_iid, approved_count, required, author_username, project_name,
        )
        state.record_violation(
            operator=author_username, operator_name=author_name,
            violation_type="mr_merged_without_approval",
            description=f"MR !{mr_iid}「{mr_title}」审批不足即合并（{approved_count}/{required}），可能绕过 ccg 门禁",
            project=project_name,
            detail={"mr_iid": mr_iid, "approved_count": approved_count, "required": required,
                    "source_branch": source_branch, "mr_url": mr_url},
        )
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        return "ok"

    return "ignored"


def handle_mr_event(payload: dict[str, Any], config: Config) -> str:
    attrs = payload.get("object_attributes", {}) or {}
    action = attrs.get("action", "")

    if action in ("open", "reopen"):
        return _on_mr_open(payload, attrs, config)

    if action == "merge":
        return _on_mr_merged(payload, attrs, config)

    if action == "update":
        return _on_mr_update(payload, attrs, config)

    if action == "unapproved":
        return "ok" if _update_frontend_review_status(config, payload, attrs) else "ignored"

    if action != "approved":
        logger.info("MR event action=%s, ignored", action)
        return "ignored"

    mr_iid = attrs.get("iid", "?")
    mr_title = attrs.get("title", "")
    mr_url = attrs.get("url", "")
    source_branch: str = attrs.get("source_branch", "")
    target_branch: str = attrs.get("target_branch", "")

    # 审批人
    user = payload.get("user", {}) or {}
    approver_name = user.get("name", "")
    approver_username = user.get("username", "")

    # 研发（MR assignee）—— 兼容新旧 GitLab payload 格式
    assignees: list[dict] = payload.get("assignees") or []
    if not assignees:
        single = payload.get("assignee") or {}
        if single:
            assignees = [single]

    project = payload.get("project", {}) or {}
    project_name = project.get("name", "")
    project_id = project.get("id")

    _update_frontend_review_status(config, payload, attrs)

    # payload 里 assignees 为空时，调 API 补全（部分 GitLab 版本 approved 事件不带 assignees）
    if not assignees and config.gitlab_client and project_id:
        mr_detail = config.gitlab_client.get_mr(project_id, mr_iid)
        assignees = mr_detail.get("assignees") or []
        if not assignees:
            single = mr_detail.get("assignee") or {}
            if single:
                assignees = [single]

    assignee_names = [a.get("name", "") for a in assignees if a.get("name")]
    assignee_usernames = [a.get("username", "") for a in assignees if a.get("username")]
    developer_label = "、".join(assignee_names) if assignee_names else "研发"

    logger.info(
        "MR approved event mr=!%s source=%s target=%s approver=%s assignees=%s project=%s",
        mr_iid, source_branch, target_branch, approver_username, assignee_usernames, project_name,
    )

    # 判断 MR 类型
    is_feature = _is_feature_branch(source_branch)
    is_hotfix = _is_hotfix_branch(source_branch)
    is_quickfix = _is_quickfix_branch(source_branch)
    is_release = source_branch == config.pre_branch

    at_mobiles = config.resolve_wechat_ids(assignee_usernames)

    if is_quickfix:
        content = (
            f"### 快速迭代 MR 审批通过 ✓\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **审批人**：{approver_name}\n"
            f"> **合并目标**：`{target_branch}`\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**下一步 · {developer_label}**\n"
            f"> 审批已通过，执行以下命令合并到 `{target_branch}`：\n"
            f"> `ccg gitlab mr merge {mr_iid}`"
        )

    elif is_feature:
        content = (
            f"### 需求 MR 审批通过 ✓\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **审批人**：{approver_name}\n"
            f"> **合并目标**：`{target_branch}`\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**下一步 · {developer_label}**\n"
            f"> 审批已通过，执行以下命令合并到 `{target_branch}`：\n"
            f"> `ccg gitlab mr merge {mr_iid}`"
        )

    elif is_hotfix:
        if target_branch == config.main_branch:
            # 紧急路径：直接上线，需多人审批，额外 @ TL
            required = config.hotfix_required_approvals
            content = (
                f"### 🚨 热修 MR 获得新 Approve\n"
                f"> **MR !{mr_iid}**：{mr_title}\n"
                f"> **审批人**：{approver_name}\n"
                f"> **合并目标**：`{target_branch}`（直接上线）\n"
                f"> [查看 MR]({mr_url})\n\n"
                f"**下一步 · {developer_label}**\n"
                f"> 热修 MR 需要 **{required} 人** Approve，先确认当前人数是否满足：\n"
                f"> `ccg gitlab mr check {mr_iid}`\n"
                f"> 满足后执行：\n"
                f"> `ccg gitlab mr merge {mr_iid}`"
            )
            at_mobiles = list(dict.fromkeys(at_mobiles + config.tl_mobiles))
        else:
            # 非紧急路径：合入 pre，随下次上线一起发布，1 人审批即可
            content = (
                f"### 热修 MR 审批通过 ✓\n"
                f"> **MR !{mr_iid}**：{mr_title}\n"
                f"> **审批人**：{approver_name}\n"
                f"> **合并目标**：`{target_branch}`（随下次上线发布）\n"
                f"> [查看 MR]({mr_url})\n\n"
                f"**下一步 · {developer_label}**\n"
                f"> 审批已通过，执行以下命令合并到 `{target_branch}`：\n"
                f"> `ccg gitlab mr merge {mr_iid}`"
            )

    elif is_release:
        content = (
            f"### 上线 MR 审批通过 ✓\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **审批人**：{approver_name}\n"
            f"> `pre → {target_branch}` 上线 MR 已通过\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**下一步 · {developer_label}**\n"
            f"> TL 已批准，执行以下命令合并上线：\n"
            f"> `ccg gitlab mr merge {mr_iid}`"
        )

    else:
        content = (
            f"### MR 审批通过 ✓\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **审批人**：{approver_name}\n"
            f"> `{source_branch} → {target_branch}`\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**下一步 · {developer_label}**\n"
            f"> 审批已通过，执行以下命令完成合并：\n"
            f"> `ccg gitlab mr merge {mr_iid}`"
        )

    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)

    # 如果是热修 MR (hotfix_* → main)，更新 Commit Status
    if is_hotfix and target_branch == config.main_branch:
        _update_hotfix_mr_status(config, payload, attrs)

    return "ok"


# ──────────────────────────────────────────────────────────────
# Note Hook（Issue 评论）
# 覆盖的通知节点：
#   product:pass   — 产品 pre 验收通过，通知研发
#   product:reject — 产品 pre 验收拒绝，通知研发
#   developer:pass — 研发 pre 自测通过，通知产品
#   developer:reject — 研发 pre 自测拒绝，通知产品
# ──────────────────────────────────────────────────────────────

def _normalize_colon(text: str) -> str:
    """全角冒号 ：(U+FF1A) → ASCII 冒号，统一口令匹配（与 ai-workflow 保持一致）。"""
    return text.replace("：", ":")


# 匹配四种验收口令（含过去时变体 passed/rejected）：
#   product / developer  ×  pass(ed) / reject(ed)
_VERDICT_PATTERN = re.compile(
    r"\b(?P<role>product|developer):(?P<verdict>pass(?:ed)?|reject(?:ed)?)\b",
    re.IGNORECASE,
)


def _normalize_verdict(raw: str) -> str:
    """'passed'→'pass'，'rejected'→'reject'，统一为动词原形。"""
    lower = raw.lower()
    if lower.startswith("pass"):
        return "pass"
    return "reject"


def handle_note_event(payload: dict[str, Any], config: Config) -> str:
    attrs = payload.get("object_attributes", {}) or {}
    noteable_type = attrs.get("noteable_type", "")

    if noteable_type != "Issue":
        logger.info("Ignoring note event noteable_type=%s", noteable_type)
        return "ignored"

    if attrs.get("system"):
        return "ignored"

    note_body: str = attrs.get("note", "")
    note_url = attrs.get("url", "")
    # 全角冒号归一化后用于匹配，原始 note_body 用于展示
    normalized_body = _normalize_colon(note_body)

    project = payload.get("project", {}) or {}
    project_name = project.get("name", "")
    project_id = project.get("id")

    user = payload.get("user", {}) or {}
    commenter_name = user.get("name", "")
    commenter_username = user.get("username", "")

    issue = payload.get("issue", {}) or {}
    issue_iid = issue.get("iid", "?")
    issue_title = issue.get("title", "")

    assignees: list[dict] = issue.get("assignees") or []
    assignee_names = [a.get("name", "") for a in assignees if a.get("name")]
    assignee_usernames = [a.get("username", "") for a in assignees if a.get("username")]

    # GitLab Note Hook 中 issue.author 字段视版本而定，防御性读取
    reporter = issue.get("author") or {}
    reporter_name = reporter.get("name", "")
    reporter_username = reporter.get("username", "")

    verdict_match = _VERDICT_PATTERN.search(normalized_body)
    if verdict_match:
        # 验收口令发生变化，检查是否需要更新相关 MR 的 Commit Status
        _update_related_mr_status(config, project_id, issue_iid)

        # 去掉口令本身，其余文字作为理由/备注传给通知
        reason = _VERDICT_PATTERN.sub("", normalized_body)
        reason = re.sub(r"[ \t]{2,}", " ", reason)   # 合并口令被删后留下的多余空格
        reason = reason.strip(" \t\n\r，。、；：！？·")
        if len(reason) > 300:
            reason = reason[:300] + "..."
        return _handle_verdict_comment(
            config=config,
            role=verdict_match.group("role").lower(),
            verdict=_normalize_verdict(verdict_match.group("verdict")),
            reason=reason,
            issue_iid=issue_iid,
            issue_title=issue_title,
            note_url=note_url,
            commenter_name=commenter_name,
            commenter_username=commenter_username,
            assignee_names=assignee_names,
            assignee_usernames=assignee_usernames,
            reporter_name=reporter_name,
            reporter_username=reporter_username,
        )

    # 普通评论：通知 assignee（不 @ 评论人自己）
    display_note = note_body if len(note_body) <= 200 else note_body[:200] + "..."
    content = (
        f"### Issue 收到新评论\n"
        f"> **项目**：{project_name}\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **评论人**：{commenter_name}\n"
        f"> **内容**：{display_note}\n"
        f"> [查看评论]({note_url})\n"
    )
    at_usernames = [u for u in assignee_usernames if u != commenter_username]
    at_mobiles = config.resolve_wechat_ids(at_usernames)

    logger.info(
        "Note event issue=#%s commenter=%s assignees=%s at_mobiles=%s",
        issue_iid, commenter_username, assignee_usernames, at_mobiles,
    )
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


def _update_related_mr_status(config: Config, project_id: int, issue_iid: int) -> None:
    """当 Issue 验收状态变化时，更新相关上线 MR 的 Commit Status。"""
    if not config.gitlab_client or not project_id:
        return

    try:
        # 查找 pre → main 的开放 MR
        mrs = config.gitlab_client._request(
            "GET",
            f"/projects/{project_id}/merge_requests",
            params={
                "state": "opened",
                "source_branch": config.pre_branch,
                "target_branch": config.main_branch,
            },
        )
        if not mrs:
            return

        # 检查当前验收状态
        incomplete = config.gitlab_client.check_pre_verification_status(project_id, config.pre_branch)

        for mr in mrs:
            mr_iid = mr.get("iid")
            commit_sha = mr.get("sha")
            if not commit_sha:
                continue

            if incomplete:
                # 仍有未完成的验收
                issue_ids = [item["issue_iid"] for item in incomplete]
                description = f"验收未完成：Issue {issue_ids} 需要 product:pass 和 developer:pass"
                config.gitlab_client.set_commit_status(
                    project_id=project_id,
                    commit_sha=commit_sha,
                    state="failed",
                    name="pre-acceptance-check",
                    description=description[:255],
                )
                logger.info("Updated MR !%s status to failed due to incomplete acceptance", mr_iid)
            else:
                # 所有验收已完成
                config.gitlab_client.set_commit_status(
                    project_id=project_id,
                    commit_sha=commit_sha,
                    state="success",
                    name="pre-acceptance-check",
                    description="所有 Issue 验收已完成",
                )
                logger.info("Updated MR !%s status to success, all acceptance completed", mr_iid)

    except Exception as e:
        logger.warning("Failed to update related MR status for issue #%s: %s", issue_iid, e)


def _update_hotfix_mr_status(config: Config, payload: dict[str, Any], attrs: dict[str, Any]) -> None:
    """热修 MR 获得新 Approve 时，更新 Commit Status。"""
    if not config.gitlab_client:
        return

    project = payload.get("project", {}) or {}
    project_id = project.get("id")
    if not project_id:
        return

    mr_iid = attrs.get("iid")
    mr_url = attrs.get("url", "")
    commit_sha = attrs.get("last_commit", {}).get("id") if isinstance(attrs.get("last_commit"), dict) else None
    if not commit_sha:
        commit_sha = attrs.get("sha")

    if not commit_sha or not mr_iid:
        return

    try:
        # 查询当前 Approve 数量
        approvals_data = config.gitlab_client.get_mr_approvals(project_id, mr_iid)
        approved_by = approvals_data.get("approved_by") or []
        current_approvals = len(approved_by)
        required_approvals = config.hotfix_required_approvals

        if current_approvals < required_approvals:
            # Approve 仍不足 → 保持 failed
            description = f"需要 {required_approvals} 人 Approve，当前 {current_approvals} 人"
            config.gitlab_client.set_commit_status(
                project_id=project_id,
                commit_sha=commit_sha,
                state="failed",
                name="hotfix-approval-check",
                description=description,
                target_url=mr_url,
            )
            logger.info("Updated hotfix MR !%s status to failed: %s/%s approvals", mr_iid, current_approvals, required_approvals)
        else:
            # Approve 已满足 → 更新为 success
            description = f"✓ 已获得 {current_approvals} 人 Approve，满足要求"
            config.gitlab_client.set_commit_status(
                project_id=project_id,
                commit_sha=commit_sha,
                state="success",
                name="hotfix-approval-check",
                description=description,
            )
            logger.info("Updated hotfix MR !%s status to success: %s approvals", mr_iid, current_approvals)

    except Exception as e:
        logger.warning("Failed to update hotfix MR !%s status: %s", mr_iid, e)


def _handle_verdict_comment(
    config: Config,
    role: str,          # "product" or "developer"
    verdict: str,       # "pass" or "reject"
    reason: str,        # 口令以外的附加文字，可为空
    issue_iid,
    issue_title: str,
    note_url: str,
    commenter_name: str,
    commenter_username: str,
    assignee_names: list[str],
    assignee_usernames: list[str],
    reporter_name: str,
    reporter_username: str,
) -> str:
    is_pass = verdict == "pass"
    developer_label = "、".join(assignee_names) if assignee_names else "研发"
    product_label = reporter_name or "产品"
    issue_ref = f"Issue #{issue_iid}"

    # 理由/备注块：reject 标"原因"，pass 标"备注"，无附加文字则不显示
    reason_label = "原因" if not is_pass else "备注"
    if reason:
        # 多行理由：每行加 > 前缀
        reason_lines = "\n".join(
            f"> {line}" for line in reason.splitlines() if line.strip()
        )
        reason_block = f"> **{reason_label}**：\n{reason_lines}\n"
    else:
        reason_block = ""

    if role == "product":
        # 产品评论 → 通知研发
        if is_pass:
            title = "### 产品 pre 验收通过 ✓"
            next_steps = (
                f"**下一步 · {developer_label}（研发）**\n"
                f"> 产品已完成验收，请你也登录 pre 环境完成自测\n"
                f"> 通过后在 Issue 评论区回复：`developer:pass`\n"
                f"> 双方均通过后执行：`ccg gitlab mr release`"
            )
        else:
            title = "### 🚫 产品验收阻断上线"
            next_steps = (
                f"**下一步 · {developer_label}（研发）**\n"
                f"> 产品验收不通过，本次上线已被阻断\n"
                f"> 请根据上方原因修复后重新合入 pre\n"
                f"> 修复完成后双方需重新在 Issue 评论区发布验收口令"
            )
        at_mobiles = config.resolve_wechat_ids(assignee_usernames)
        role_label = "产品"

    else:  # developer
        # 研发评论 → 通知产品
        if is_pass:
            title = "### 研发 pre 自测通过 ✓"
            next_steps = (
                f"**下一步 · {product_label}（产品）**\n"
                f"> 研发已完成自测，请你登录 pre 环境完成功能验收\n"
                f"> 通过后在 Issue 评论区回复：`product:pass`\n"
                f"> 双方均通过后研发会执行：`ccg gitlab mr release`"
            )
        else:
            title = "### 🚫 研发自测阻断上线"
            next_steps = (
                f"**下一步 · {product_label}（产品）**\n"
                f"> 研发自测发现问题，本次上线已被阻断，暂无需产品操作\n"
                f"> 等待研发修复后重新合入 pre，届时会再次通知验收"
            )
        at_mobiles = config.resolve_wechat_ids(
            [reporter_username] if reporter_username else []
        )
        role_label = "研发"

    content = (
        f"{title}\n"
        f"> **{issue_ref}**：{issue_title}\n"
        f"> **{role_label}**：{commenter_name}\n"
        f"{reason_block}"
        f"> [查看评论]({note_url})\n\n"
        f"{next_steps}"
    )

    logger.info(
        "Verdict note issue=#%s role=%s verdict=%s commenter=%s at_mobiles=%s",
        issue_iid, role, verdict, commenter_username, at_mobiles,
    )
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


# ──────────────────────────────────────────────────────────────
# Push Hook
# 告警节点：有人直接 push 到受保护分支（main_branch / pre_branch），绕过 MR 审批流程
# ──────────────────────────────────────────────────────────────

def handle_push_event(payload: dict[str, Any], config: Config) -> str:
    ref: str = payload.get("ref", "")
    branch = ref.removeprefix("refs/heads/")
    is_force = payload.get("push_force", False)

    pusher_name = payload.get("user_name", "")
    pusher_username = payload.get("user_username", "")
    project = payload.get("project", {}) or {}
    project_name = project.get("name", "")
    project_url = project.get("web_url", "")
    commits: list[dict] = payload.get("commits") or []
    total = payload.get("total_commits_count", len(commits))
    at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([pusher_username])))

    commit_lines = ""
    for c in commits[:5]:
        title = (c.get("message") or "").splitlines()[0][:80]
        commit_lines += f"> - [{title}]({c.get('url', '')})\n"
    if total > 5:
        commit_lines += f"> - ...共 {total} 个提交\n"

    notified = False

    # 强制推送：个人功能/热修分支 rebase 后 ccg 会 force-with-lease，属正常操作，跳过
    if is_force and _is_feature_or_hotfix_branch(branch):
        logger.info("Force push to feature/hotfix branch %s (likely post-rebase), skipped", branch)
    elif is_force:
        content = (
            f"### 🚨 强制推送（Force Push）告警\n"
            f"> **项目**：{project_name}\n"
            f"> **分支**：`{branch}`\n"
            f"> **操作人**：{pusher_name}（`{pusher_username}`）\n"
            f"> [查看项目]({project_url})\n\n"
            f"**警告**：强制推送会覆盖历史提交，可能导致他人代码丢失，请立即确认影响范围。"
        )
        logger.warning("Force push branch=%s pusher=%s project=%s", branch, pusher_username, project_name)
        state.record_violation(
            operator=pusher_username, operator_name=pusher_name,
            violation_type="force_push",
            description=f"强制推送到分支 {branch}",
            project=project_name,
            detail={"branch": branch, "project_url": project_url},
        )
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        notified = True

    # 直接推送到受保护分支（未经 MR）
    # MR 合并也会触发 Push Hook，通过以下方式排除：
    # 1. merge commit 中包含 "See merge request"（普通 merge）
    # 2. pusher 为 project bot（fast-forward/rebase merge 由 bot 执行）
    is_mr_merge = (
        any("See merge request" in (c.get("message") or "") for c in commits)
        or "bot" in pusher_username
    )
    if branch in {config.main_branch, config.pre_branch} and not is_mr_merge:
        content = (
            f"### ⚠️ 直接 Push 到受保护分支\n"
            f"> **项目**：{project_name}\n"
            f"> **分支**：`{branch}`\n"
            f"> **操作人**：{pusher_name}（`{pusher_username}`）\n"
            f"> **提交数**：{total}\n"
            f"{commit_lines}"
            f"> [查看项目]({project_url})\n\n"
            f"**注意**：`{branch}` 为受保护分支，应通过 MR 合并，请确认此次推送是否符合规范。"
        )
        logger.warning("Direct push to protected branch=%s pusher=%s commits=%s project=%s", branch, pusher_username, total, project_name)
        state.record_violation(
            operator=pusher_username, operator_name=pusher_name,
            violation_type="direct_push_protected",
            description=f"直接 push 到受保护分支 {branch}，共 {total} 个提交",
            project=project_name,
            detail={"branch": branch, "total_commits": total, "project_url": project_url},
        )
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        notified = True

    if not notified:
        logger.info("Push to non-protected branch=%s force=%s, ignored", branch, is_force)
        return "ignored"

    return "ok"
