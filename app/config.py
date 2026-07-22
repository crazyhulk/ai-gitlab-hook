from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .gitlab_client import GitLabClient


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class LogConfig:
    level: str = "INFO"
    dir: str = "./logs"
    file: str = "gitlab-hook.log"


@dataclass
class WechatConfig:
    webhook_url: str = ""


@dataclass
class GitlabConfig:
    secret_token: str = ""
    url: str = ""
    token: str = ""


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    log: LogConfig = field(default_factory=LogConfig)
    wechat: WechatConfig = field(default_factory=WechatConfig)
    gitlab: GitlabConfig = field(default_factory=GitlabConfig)
    # gitlab_username -> 企业微信手机号
    user_map: dict[str, str] = field(default_factory=dict)
    # Reviewer / TL 的 GitLab 用户名列表，用于热修/上线时额外 @ 通知
    tl_usernames: list[str] = field(default_factory=list)
    # 分支名配置（与 ai-workflow 保持一致）
    main_branch: str = "main"
    pre_branch: str = "pre"
    # 热修 MR 所需最少 Approve 人数（需求 MR 固定为 1；hotfix→pre 固定为 1）
    hotfix_required_approvals: int = 2
    # 修改指定前端目录时，至少需要其中一名 Reviewer Approve
    frontend_review_path: str = "frontend-v1/"
    frontend_required_reviewers: list[str] = field(
        default_factory=lambda: ["yangzhengpeng01", "wangqiyuan01"]
    )
    # hotfix 合入 main 后，超过此小时数未同步 pre 则触发告警
    hotfix_sync_threshold_hours: int = 4
    # 内部定时检查热修同步状态的间隔秒数（默认每分钟一次）
    hotfix_sync_check_interval_seconds: int = 60
    # 由 __post_init__ 按 gitlab.url/token 自动初始化，无需手动设置
    gitlab_client: GitLabClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.gitlab.url and self.gitlab.token:
            from .gitlab_client import GitLabClient
            self.gitlab_client = GitLabClient(self.gitlab.url, self.gitlab.token)

    def resolve_wechat_ids(self, gitlab_usernames: list[str]) -> list[str]:
        seen: set[str] = set()
        result = []
        for u in gitlab_usernames:
            mid = self.user_map.get(u, "")
            if mid and mid not in seen:
                seen.add(mid)
                result.append(mid)
        return result

    @property
    def tl_mobiles(self) -> list[str]:
        return self.resolve_wechat_ids(self.tl_usernames)


def load_config(path: str = "config.yaml") -> Config:
    data: dict = {}
    config_path = Path(path)
    if config_path.is_file():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    s = data.get("server", {}) or {}
    lg = data.get("log", {}) or {}
    w = data.get("wechat", {}) or {}
    g = data.get("gitlab", {}) or {}
    user_map: dict[str, str] = {
        str(k): str(v) for k, v in (data.get("user_map") or {}).items()
    }

    # 环境变量 WECHAT_USER_<gitlab_username>=<mobile> 覆盖 user_map
    prefix = "WECHAT_USER_"
    for k, v in os.environ.items():
        if k.startswith(prefix) and v.strip():
            user_map[k[len(prefix):]] = v.strip()

    # tl_usernames: yaml list 或环境变量 TL_USERNAMES=user1,user2
    tl_raw = data.get("tl_usernames") or []
    if isinstance(tl_raw, list):
        tl_usernames = [str(x) for x in tl_raw if x]
    else:
        tl_usernames = [x.strip() for x in str(tl_raw).split(",") if x.strip()]
    env_tl = os.environ.get("TL_USERNAMES", "")
    if env_tl:
        tl_usernames = [x.strip() for x in env_tl.split(",") if x.strip()]

    frontend_reviewers_raw = data.get("frontend_required_reviewers") or [
        "yangzhengpeng01",
        "wangqiyuan01",
    ]
    if isinstance(frontend_reviewers_raw, list):
        frontend_required_reviewers = [str(x) for x in frontend_reviewers_raw if x]
    else:
        frontend_required_reviewers = [
            x.strip() for x in str(frontend_reviewers_raw).split(",") if x.strip()
        ]
    env_frontend_reviewers = os.environ.get("FRONTEND_REQUIRED_REVIEWERS", "")
    if env_frontend_reviewers:
        frontend_required_reviewers = [
            x.strip() for x in env_frontend_reviewers.split(",") if x.strip()
        ]

    frontend_review_path = os.environ.get(
        "FRONTEND_REVIEW_PATH",
        str(data.get("frontend_review_path", "frontend-v1/")),
    ).strip()
    if frontend_review_path and not frontend_review_path.endswith("/"):
        frontend_review_path += "/"

    main_branch = os.environ.get("GITLAB_BRANCH_MAIN", str(data.get("main_branch", "main")))
    pre_branch = os.environ.get("GITLAB_BRANCH_PRE", str(data.get("pre_branch", "pre")))

    hotfix_required_approvals = int(
        os.environ.get(
            "HOTFIX_REQUIRED_APPROVALS",
            str(data.get("hotfix_required_approvals", 2)),
        )
    )

    hotfix_sync_threshold_hours = int(
        os.environ.get(
            "HOTFIX_SYNC_THRESHOLD_HOURS",
            str(data.get("hotfix_sync_threshold_hours", 4)),
        )
    )

    hotfix_sync_check_interval_seconds = int(
        os.environ.get(
            "HOTFIX_SYNC_CHECK_INTERVAL",
            str(data.get("hotfix_sync_check_interval_seconds", 3600)),
        )
    )

    return Config(
        server=ServerConfig(
            host=str(s.get("host", "0.0.0.0")),
            port=int(s.get("port", 8080)),
        ),
        log=LogConfig(
            level=os.environ.get("LOG_LEVEL", str(lg.get("level", "INFO"))),
            dir=os.environ.get("LOG_DIR", str(lg.get("dir", "./logs"))),
            file=os.environ.get("LOG_FILE", str(lg.get("file", "gitlab-hook.log"))),
        ),
        wechat=WechatConfig(
            webhook_url=os.environ.get(
                "WECHAT_WEBHOOK_URL", str(w.get("webhook_url", ""))
            ),
        ),
        gitlab=GitlabConfig(
            secret_token=os.environ.get(
                "GITLAB_WEBHOOK_SECRET", str(g.get("secret_token", ""))
            ),
            url=os.environ.get("GITLAB_URL", str(g.get("url", ""))),
            token=os.environ.get("GITLAB_PRIVATE_TOKEN", str(g.get("token", ""))),
        ),
        user_map=user_map,
        tl_usernames=tl_usernames,
        main_branch=main_branch,
        pre_branch=pre_branch,
        hotfix_required_approvals=hotfix_required_approvals,
        frontend_review_path=frontend_review_path,
        frontend_required_reviewers=frontend_required_reviewers,
        hotfix_sync_threshold_hours=hotfix_sync_threshold_hours,
        hotfix_sync_check_interval_seconds=hotfix_sync_check_interval_seconds,
    )
