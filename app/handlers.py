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
_BUG_SECTIONS = ["问题现象", "复现步骤", "预期正常结果", "实际异常结果", "出现环境", "严重等级"]


def _missing_sections(body: str, sections: list[str]) -> list[str]:
    return [s for s in sections if not re.search(rf"##\s*{re.escape(s)}", body or "")]


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

    is_feature = issue_title.startswith("【需求】")
    is_bug = issue_title.startswith("【Bug】") or issue_title.startswith("【bug】")

    logger.info(
        "Issue event action=%s issue=#%s title=%r project=%s reporter=%s assignees=%s",
        action, issue_iid, issue_title, project_name, reporter_username, assignee_usernames,
    )

    if action == "open":
        if is_feature:
            return _on_feature_issue_open(
                config, issue_iid, issue_title, issue_url, description,
                reporter_name, reporter_username, assignee_names, assignee_usernames,
            )
        if is_bug:
            return _on_bug_issue_open(
                config, issue_iid, issue_title, issue_url, description,
                reporter_name, reporter_username, assignee_names, assignee_usernames,
            )
        logger.info("Issue #%s has no workflow prefix, skipping", issue_iid)
        return "ignored"

    if action == "update":
        if is_feature or is_bug:
            return _on_issue_update(
                config, issue_iid, issue_title, issue_url, description,
                reporter_name, reporter_username, assignee_names, assignee_usernames,
                is_bug=is_bug,
            )
        return "ignored"

    logger.info("Issue event action=%s not handled", action)
    return "ignored"


def _on_feature_issue_open(
    config: Config,
    issue_iid, issue_title, issue_url, description,
    reporter_name, reporter_username, assignee_names, assignee_usernames,
) -> str:
    missing = _missing_sections(description, _FEATURE_SECTIONS)
    if missing:
        # 格式不合规 → 通知产品补充，记录到状态机
        state.mark_issue_invalid(int(issue_iid))
        missing_str = "、".join(missing)
        content = (
            f"### 需求 Issue 格式不合规\n"
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
        logger.info("Feature issue #%s invalid missing=%s, notifying reporter=%s", issue_iid, missing, reporter_username)
        send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
        return "ok"

    # 格式合规 → 通知研发认领
    assignee_str = "、".join(assignee_names) if assignee_names else "（待指派）"
    content = (
        f"### 新需求 Issue 待认领\n"
        f"> **Issue #{issue_iid}**：{issue_title}\n"
        f"> **提出人**：{reporter_name}\n"
        f"> **负责人**：{assignee_str}\n"
        f"> [查看 Issue]({issue_url})\n\n"
        f"**下一步 · {assignee_str or '研发'}**\n"
        f"> 请阅读需求后执行以下命令认领并拉取功能分支：\n"
        f"> `ccg gitlab feature start {issue_iid}`"
    )
    at_mobiles = config.resolve_wechat_ids(assignee_usernames)
    logger.info("Feature issue #%s created, notifying assignees=%s", issue_iid, assignee_usernames)
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
) -> str:
    if not state.is_issue_known_invalid(int(issue_iid)):
        # 之前未记录为不合规，忽略（避免每次更新都发通知）
        logger.info("Issue #%s updated but not previously marked invalid, skipping", issue_iid)
        return "ignored"

    sections = _BUG_SECTIONS if is_bug else _FEATURE_SECTIONS
    missing = _missing_sections(description, sections)
    if missing:
        # 补充后仍不合规，继续等待
        logger.info("Issue #%s updated but still invalid missing=%s, no notification", issue_iid, missing)
        return "ignored"

    # 从不合规变为合规 → 通知研发重新认领
    state.mark_issue_valid(int(issue_iid))
    cmd = f"ccg gitlab hotfix start {issue_iid}" if is_bug else f"ccg gitlab feature start {issue_iid}"
    issue_type = "Bug" if is_bug else "需求"
    assignee_str = "、".join(assignee_names) if assignee_names else "（待指派）"
    content = (
        f"### {issue_type} Issue 格式已补全，可以认领\n"
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
# ──────────────────────────────────────────────────────────────

def handle_mr_event(payload: dict[str, Any], config: Config) -> str:
    attrs = payload.get("object_attributes", {}) or {}
    action = attrs.get("action", "")

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

    logger.info(
        "Verdict note issue=#%s role=%s verdict=%s commenter=%s at_mobiles=%s",
        issue_iid, role, verdict, commenter_username, at_mobiles,
    )
    send_webhook(config.wechat.webhook_url, content, at_mobiles=at_mobiles)
    return "ok"
