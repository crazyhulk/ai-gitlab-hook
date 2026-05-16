import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


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
    # 热修 MR 所需最少 Approve 人数（需求 MR 固定为 1）
    hotfix_required_approvals: int = 2

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

    hotfix_required_approvals = int(
        os.environ.get(
            "HOTFIX_REQUIRED_APPROVALS",
            str(data.get("hotfix_required_approvals", 2)),
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
        ),
        user_map=user_map,
        tl_usernames=tl_usernames,
        hotfix_required_approvals=hotfix_required_approvals,
    )
