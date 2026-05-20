# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GitLab Webhook 接收服务（Python/FastAPI），将 GitLab 事件转化为企业微信群通知，并通过 Commit Status API 实现 MR 合并门禁。使用 SQLite 持久化违规记录和热修同步状态。

## Commands

```bash
# 启动/停止/重启/状态
./service.sh start
./service.sh stop
./service.sh restart
./service.sh status

# 本地开发（带热重载）
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8021 --reload

# 安装依赖
pip install -r requirements.txt
```

无测试框架，无 lint 配置。

## Architecture

```
run.py              — 入口，加载配置并启动 uvicorn
service.sh          — 生产环境 nohup 守护脚本（start/stop/restart/status）
app/
  main.py           — FastAPI app，lifespan 初始化配置和热修同步定时任务
  webhook.py        — 路由定义：/gitlab/webhook, /health, /violations, /check-hotfix-sync
  handlers.py       — 核心业务逻辑（1478 行），处理 Issue/MR/Note/Push 四类事件
  config.py         — dataclass 配置，支持 config.yaml + 环境变量覆盖
  gitlab_client.py  — GitLab API 封装（urllib，无第三方 HTTP 库）
  wechat.py         — 企业微信 webhook 发送（先 @ 再发 markdown）
  state.py          — SQLite 状态管理（violations 表 + hotfix_sync_pending 表）
  logger.py         — 日志配置，支持 request_id 上下文
config.yaml         — 运行时配置（不入库，从 config.yaml.example 复制）
```

## Key Design Decisions

- 零第三方 HTTP 依赖：GitLab API 和企业微信调用均使用 stdlib `urllib`
- 配置优先级：环境变量 > config.yaml > 代码默认值
- 企业微信通知格式：先发一条 text 消息 @ 手机号，再发 markdown 正文（企微限制 markdown 不支持 @）
- 门禁通过 GitLab Commit Status API 实现，无法在 GitLab 页面绕过
- `handlers.py` 是单文件大模块，所有事件处理逻辑集中于此，按 Issue/MR/Note/Push 分区
- Issue 类型通过 description 中的章节标题识别（`## 需求背景` / `## 优化背景` / `## 问题现象`）
- 分支命名同时支持新格式（`feature/`、`hotfix/`）和旧格式（`issue_`、`hotfix_`）

## Configuration

复制 `config.yaml.example` 为 `config.yaml`，关键配置项：
- `gitlab.secret_token` — webhook 验证 token
- `gitlab.url` / `gitlab.token` — GitLab API 访问（用于门禁和验收检查）
- `wechat.webhook_url` — 企业微信机器人 webhook
- `user_map` — GitLab 用户名到企微手机号的映射
- `tl_usernames` — TL 列表，用于额外 @ 通知
