from __future__ import annotations

import re
from typing import Any

from . import state
from .config import Config
from .logger import get_logger
from .wechat import send_webhook

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────
# Issue 格式校验（与 ai-workflow/cc/validator.py 保持一致）
# ──────────────────────────────────────────────────────────────

_FEATURE_SECTIONS = ["需求背景", "功能详细描述", "验收标准", "优先级"]
_IMPROVE_SECTIONS = ["优化背景", "现状问题", "优化方案", "预期收益", "优先级"]
_BUG_SECTIONS = ["问题现象", "复现步骤", "预期正常结果", "实际异常结果", "出现环境", "严重等级"]


def _missing_sections(body: str, sections: list[str]) -> list[str]:
    return [s for s in sections if not re.search(rf"##\s*{re.escape(s)}", body or "")]


def _detect_issue_type(description: str) -> str | None:
    """根据 description 中的唯一标识章节推断 issue 类型。"""
    body = description or ""
    if re.search(r"##\s*优化背景", body):
        return "improve"
    if re.search(r"##\s*需求背景", body):
        return "feature"
    if re.search(r"##\s*问题现象", body):
        return "bug"
    return None


# ──────────────────────────────────────────────────────────────
# Issue Hook
# 覆盖的人工通知节点：
#   节点1 — 新需求 Issue 创建后，产品通知研发认领
#   节点2 — Issue 格式不合规补充后，产品通知研发重新认领
#   节点6 — 线上 Bug Issue 创建后，产品通知研发+TL 热修
# ──────────────────────────────────────────────────────────────

def handle_issue_event(payload: dict[str, Any], config: Config) -> str:
    attrs = payload.get("object_attributes", {}) or {}
    action = attrs.get("action", "")
    if not action:
        issue_state = attrs.get("state", "")
        changes = payload.get("changes") or {}
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
    is_feature = issue_type in ("feature", "improve")
    is_bug = issue_type == "bug"

    logger.info(
        "Issue event action=%s issue=#%s type=%s title=%r project=%s reporter=%s assignees=%s",
        action, issue_iid, issue_type, issue_title, project_name, reporter_username, assignee_usernames,
    )

    if action == "open":
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
            return _on_issue_update(
                config, issue_iid, issue_title, issue_url, description,
                reporter_name, reporter_username, assignee_names, assignee_usernames,
                is_bug=is_bug,
                issue_type=issue_type,
            )
        return "ignored"

    if action == "close":
        if not assignees and issue_type in ("feature", "improve", "bug"):
            project = payload.get("project", {}) or {}
            project_name = project.get("name", "")
            type_label = {"bug": "Bug", "improve": "优化"}.get(issue_type or "", "需求")
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
            return "ok"
        return "ignored"

    logger.info("Issue event action=%s not handled", action)
    return "ignored"


def _on_feature_issue_open(
    config: Config,
    issue_iid, issue_title, issue_url, description,
    reporter_name, reporter_username, assignee_names, assignee_usernames,
    issue_type: str = "feature",
) -> str:
    sections = _IMPROVE_SECTIONS if issue_type == "improve" else _FEATURE_SECTIONS
    type_label = "优化" if issue_type == "improve" else "需求"
    missing = _missing_sections(description, sections)
    if missing:
        state.mark_issue_invalid(int(issue_iid))
        missing_str = "、".join(missing)
        content = (
            f"### {type_label} Issue 格式不合规\n"
            f"> **Issue #{issue_iid}**：{issue_title}\n"
            f"> **提出人**：{reporter_name}\n"
            f"> [查看 Issue]({issue_url})\n\n"
            f"**不合规详情**\n"
            f"> 缺少必填小节：{missing_str}\n\n"
            f"**下一步 · {reporter_name}（产品）**\n"
            f"> 请按标准模板补充以上小节内容\n"
            f"> 补充完成后研发即可执行：`ccg gitlab feature start {issue_iid}`"
        )
        at_mobiles = config.resolve_wechat_ids([reporter_username])
        logger.info("Feature/improve issue #%s invalid missing=%s, notifying reporter=%s", issue_iid, missing, reporter_username)
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        return "ok"

    # 格式合规 → 通知研发认领
    assignee_str = "、".join(assignee_names) if assignee_names else "（待指派）"
    content = (
        f"### 新{type_label} Issue 待认领\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **提出人**：{reporter_name}\n"
        f"> **负责人**：{assignee_str}\n"
        f"> [查看 Issue]({issue_url})\n\n"
        f"**下一步 · {assignee_str or '研发'}**\n"
        f"> 请阅读{type_label}后执行以下命令认领并拉取功能分支：\n"
        f"> `ccg gitlab feature start {issue_iid}`"
    )
    at_mobiles = config.resolve_wechat_ids(assignee_usernames)
    logger.info("Feature/improve issue #%s created, notifying assignees=%s", issue_iid, assignee_usernames)
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


def _on_bug_issue_open(
    config: Config,
    issue_iid, issue_title, issue_url, description,
    reporter_name, reporter_username, assignee_names, assignee_usernames,
) -> str:
    missing = _missing_sections(description, _BUG_SECTIONS)
    if missing:
        state.mark_issue_invalid(int(issue_iid))
        missing_str = "、".join(missing)
        content = (
            f"### Bug Issue 格式不合规\n"
            f"> **Issue #{issue_iid}**：{issue_title}\n"
            f"> **提出人**：{reporter_name}\n"
            f"> [查看 Issue]({issue_url})\n\n"
            f"**不合规详情**\n"
            f"> 缺少必填小节：{missing_str}\n\n"
            f"**下一步 · {reporter_name}（产品）**\n"
            f"> 请补充完整以上小节内容，Bug 越描述清楚修复越快\n"
            f"> 补充完成后研发即可执行：`ccg gitlab hotfix start {issue_iid}`"
        )
        at_mobiles = config.resolve_wechat_ids([reporter_username])
        logger.info("Bug issue #%s invalid missing=%s, notifying reporter=%s", issue_iid, missing, reporter_username)
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


def _on_issue_update(
    config: Config,
    issue_iid, issue_title, issue_url, description,
    reporter_name, reporter_username, assignee_names, assignee_usernames,
    is_bug: bool,
    issue_type: str | None = None,
) -> str:
    if not state.is_issue_known_invalid(int(issue_iid)):
        logger.info("Issue #%s updated but not previously marked invalid, skipping", issue_iid)
        return "ignored"

    if is_bug:
        sections = _BUG_SECTIONS
    elif issue_type == "improve":
        sections = _IMPROVE_SECTIONS
    else:
        sections = _FEATURE_SECTIONS
    missing = _missing_sections(description, sections)
    if missing:
        logger.info("Issue #%s updated but still invalid missing=%s, no notification", issue_iid, missing)
        return "ignored"

    # 从不合规变为合规 → 通知研发重新认领
    state.mark_issue_valid(int(issue_iid))
    cmd = f"ccg gitlab hotfix start {issue_iid}" if is_bug else f"ccg gitlab feature start {issue_iid}"
    type_label = "Bug" if is_bug else ("优化" if issue_type == "improve" else "需求")
    assignee_str = "、".join(assignee_names) if assignee_names else "（待指派）"
    content = (
        f"### {type_label} Issue 格式已补全，可以认领\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **提出人**：{reporter_name}\n"
        f"> **负责人**：{assignee_str}\n"
        f"> [查看 Issue]({issue_url})\n\n"
        f"**下一步 · {assignee_str or '研发'}**\n"
        f"> 产品已补充完整 Issue 格式，执行以下命令认领：\n"
        f"> `{cmd}`"
    )
    at_mobiles = config.resolve_wechat_ids(assignee_usernames or [reporter_username])
    logger.info("Issue #%s became valid, notifying assignees=%s", issue_iid, assignee_usernames)
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


# ──────────────────────────────────────────────────────────────
# Merge Request Hook
# 覆盖的人工通知节点：
#   节点3 — 需求 MR Approve 后，Reviewer 通知研发可合并
#   节点4 — 上线 MR Approve 后，TL 通知研发可上线
#   节点5 — 热修 MR Approve 后，Reviewer 通知研发可合并
# 违规检测：
#   - 功能分支（issue_*）直接合入 main/master，绕过 pre
#   - 热修分支（hotfix_*）目标分支为 pre 而非 main/master
#   - 上线 MR 创建时 pre 双向验收未完成
# ──────────────────────────────────────────────────────────────

_MAIN_BRANCHES = {"main", "master"}


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

    violations_found = False

    # 功能分支直接合入 main，跳过 pre
    if source_branch.startswith("issue_") and target_branch in _MAIN_BRANCHES:
        content = (
            f"### ⚠️ 功能分支直接合入 {target_branch}，违反上线流程\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **操作人**：{author_name}（`{author_username}`）\n"
            f"> `{source_branch}` → `{target_branch}`\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**注意**：功能分支应先合入 `pre` 完成验收，再通过上线 MR 合入 `{target_branch}`。"
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

    # 热修分支目标分支错误（应合 main，却合 pre）
    if source_branch.startswith("hotfix_") and target_branch == "pre":
        content = (
            f"### ⚠️ 热修分支目标分支错误\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **操作人**：{author_name}（`{author_username}`）\n"
            f"> `{source_branch}` → `{target_branch}`（应为 `main`/`master`）\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**注意**：热修分支应直接合入 `main`/`master` 上线，请修改 MR 目标分支。"
        )
        at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
        logger.warning("MR !%s hotfix branch targeting pre author=%s project=%s", mr_iid, author_username, project_name)
        state.record_violation(
            operator=author_username, operator_name=author_name,
            violation_type="mr_hotfix_wrong_target",
            description=f"MR !{mr_iid}「{mr_title}」热修分支 {source_branch} 目标为 pre 而非 main",
            project=project_name,
            detail={"mr_iid": mr_iid, "source_branch": source_branch, "target_branch": target_branch, "mr_url": mr_url},
        )
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        violations_found = True

    # 上线 MR（pre → main），检查 pre 双向验收是否完成
    if source_branch == "pre" and target_branch in _MAIN_BRANCHES:
        incomplete = state.get_incomplete_pre_verifications()
        if incomplete:
            issue_list = "、".join(f"#{iid}" for iid in incomplete[:10])
            content = (
                f"### ⚠️ 上线 MR 创建，但 pre 验收未完成\n"
                f"> **MR !{mr_iid}**：{mr_title}\n"
                f"> **操作人**：{author_name}（`{author_username}`）\n"
                f"> [查看 MR]({mr_url})\n\n"
                f"**以下 Issue 仅完成单方验收**：{issue_list}\n\n"
                f"**注意**：上线前需 `product:pass` 和 `developer:pass` 双方均通过，请确认后再合并。"
            )
            at_mobiles = list(dict.fromkeys(config.tl_mobiles + config.resolve_wechat_ids([author_username])))
            logger.warning("Release MR !%s created but incomplete pre verifications: %s", mr_iid, incomplete)
            state.record_violation(
                operator=author_username, operator_name=author_name,
                violation_type="mr_release_unverified",
                description=f"MR !{mr_iid}「{mr_title}」上线前 pre 验收未完成，涉及 Issue: {issue_list}",
                project=project_name,
                detail={"mr_iid": mr_iid, "incomplete_issues": incomplete, "mr_url": mr_url},
            )
            send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
            violations_found = True

    return "ok" if violations_found else "ignored"


def handle_mr_event(payload: dict[str, Any], config: Config) -> str:
    attrs = payload.get("object_attributes", {}) or {}
    action = attrs.get("action", "")

    if action == "open":
        return _on_mr_open(payload, attrs, config)

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
    assignee_names = [a.get("name", "") for a in assignees if a.get("name")]
    assignee_usernames = [a.get("username", "") for a in assignees if a.get("username")]
    developer_label = "、".join(assignee_names) if assignee_names else "研发"

    project = payload.get("project", {}) or {}
    project_name = project.get("name", "")

    logger.info(
        "MR approved event mr=!%s source=%s target=%s approver=%s assignees=%s project=%s",
        mr_iid, source_branch, target_branch, approver_username, assignee_usernames, project_name,
    )

    # 判断 MR 类型
    is_feature = source_branch.startswith("issue_")
    is_hotfix = source_branch.startswith("hotfix_")
    is_release = source_branch == "pre"
    is_sync_pre = (source_branch == "main" or source_branch == "master") and target_branch.startswith("pre")

    at_mobiles = config.resolve_wechat_ids(assignee_usernames)

    if is_feature:
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
        # 热修额外 @ TL
        at_mobiles = list(dict.fromkeys(at_mobiles + config.tl_mobiles))

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

    elif is_sync_pre:
        content = (
            f"### 热修同步 MR 审批通过 ✓\n"
            f"> **MR !{mr_iid}**：{mr_title}\n"
            f"> **审批人**：{approver_name}\n"
            f"> `{source_branch} → {target_branch}` 同步 MR 已通过\n"
            f"> [查看 MR]({mr_url})\n\n"
            f"**下一步 · {developer_label}**\n"
            f"> 执行以下命令完成热修代码同步到 pre：\n"
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

    if is_pass and isinstance(issue_iid, int):
        state.mark_pre_pass(issue_iid, role)

    logger.info(
        "Verdict note issue=#%s role=%s verdict=%s commenter=%s at_mobiles=%s",
        issue_iid, role, verdict, commenter_username, at_mobiles,
    )
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"


# ──────────────────────────────────────────────────────────────
# Push Hook
# 告警节点：有人直接 push 到受保护分支（main / master / pre），绕过 MR 审批流程
# ──────────────────────────────────────────────────────────────

_PROTECTED_BRANCHES = {"main", "master", "pre"}


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

    # 强制推送：任意分支均告警
    if is_force:
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
    if branch in _PROTECTED_BRANCHES:
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
