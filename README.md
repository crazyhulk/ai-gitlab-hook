# ai-gitlab-hook

GitLab Webhook 接收服务，将 GitLab 事件转化为企业微信群消息，驱动研发协作工作流中的各个通知节点，同时对违规操作进行实时告警并持久化记录。

## 功能概览

### 工作流通知

接收 GitLab 推送的四类事件，按业务规则发送企业微信通知并 @ 对应成员：

| 事件 | 触发场景 | 通知对象 |
|------|---------|---------|
| **Issue open / reopen** | 需求/优化 Issue，格式合规（章节齐全且已指派 assignee） | 研发（assignee） |
| **Issue open / reopen** | 需求/优化 Issue，格式不合规（缺少章节或未指派 assignee） | 产品（reporter） |
| **Issue open / reopen** | Bug Issue，格式合规（章节齐全且已指派 assignee） | 研发 + TL |
| **Issue open / reopen** | Bug Issue，格式不合规（缺少章节或未指派 assignee） | 产品（reporter） |
| **Issue update** | 新增 assignee（需求/优化/Bug） | 新增的研发（assignee） |
| **Issue update** | description 变更前不合规、变更后合规且已有 assignee | 研发（assignee） |
| **MR approved** | 需求 MR（`issue_*` 分支）通过 | 研发 |
| **MR approved** | 热修 MR（`hotfix_*` → `main`，紧急路径）通过 | 研发 + TL |
| **MR approved** | 热修 MR（`hotfix_*` → `pre`，非紧急路径）通过 | 研发 |
| **MR approved** | 上线 MR（`pre` → `main`）通过 | 研发 |
| **MR approved** | 热修同步 MR（`main/master` → `pre`）通过 | 研发 |
| **Note** | Issue 评论含 `product:pass/reject` | 研发 |
| **Note** | Issue 评论含 `developer:pass/reject` | 产品 |
| **Note** | 普通 Issue 评论 | 研发（排除评论人自己） |
| **Push** | 直接 push 到受保护分支 | 研发 + TL |

### Issue 类型识别

Issue 类型根据 description 中的模板章节自动识别，无需标题前缀：

| 类型 | 识别依据（唯一章节） | 必填章节 |
|------|-------------------|---------|
| 需求 | `## 需求背景` | 需求背景、功能详细描述、验收标准、优先级、**负责人（assignee）** |
| 优化 | `## 优化背景` | 优化背景、现状问题、优化方案、预期收益、优先级、**负责人（assignee）** |
| Bug  | `## 问题现象` | 问题现象、复现步骤、预期正常结果、实际异常结果、出现环境、严重等级、**负责人（assignee）** |

章节缺失或未指派研发（assignee）均视为不合规，通知产品补充；补全后自动触发研发认领通知。

### 违规操作告警

检测以下违规行为，实时推送企业微信告警，同时写入 SQLite 持久化记录：

| 违规类型 | 触发条件 | 通知对象 |
|---------|---------|---------|
| 直接 push 受保护分支 | push 到 `main`/`master`/`pre` | 操作人 + TL |
| 强制推送（Force Push） | `push_force: true`，任意分支 | 操作人 + TL |
| 功能分支直合 main | `issue_*` → `main`/`master` 的 MR，跳过 pre 验收 | 操作人 + TL |
| 上线 MR 验收未完成 | `pre` → `main` MR 创建时实时查 GitLab，有 Issue 未完成 `product:pass` + `developer:pass` | 操作人 + TL |
| Issue 无人认领即关闭 | Issue 关闭时 assignees 为空 | TL |
| Issue 负责人被全部移除 | Issue update 时 assignees 由有变无 | TL |
| MR 缺少 Issue 关联 | `issue_*`/`hotfix_*` MR 描述无 `Closes #xxx`，未通过 `ccg mr create` 创建 | 操作人 + TL |
| MR 标题不符规范 | `issue_*` MR 标题不以 `[需求]` 开头，或 `hotfix_*` 不以 `[Bug热修]` 开头 | 操作人 + TL |
| Issue 未验收即关闭 | 需求/优化 Issue 关闭时查 GitLab API，无 `product:pass` 记录 | 操作人 + TL |
| MR 审批不足即合并 | `issue_*`/`hotfix_*` MR 合并时查 GitLab API，Approve 不足（`hotfix_*` → `main` 需 N 人，其余需 1 人） | 操作人 + TL |
| 热修未及时同步 pre | 热修 MR 合入 `main` 后超过 `hotfix_sync_threshold_hours`（默认 4 小时）未同步到 `pre` | TL |

### 热修双路径

热修分支（`hotfix_*`）支持两种合并路径，由研发在执行 `ccg gitlab mr create` 时选择：

| 路径 | 目标分支 | 适用场景 | 所需 Approve |
|------|---------|---------|------------|
| **紧急路径** | `main`（默认） | 必须立即上线 | `hotfix_required_approvals`（默认 2） |
| **非紧急路径** | `pre` | 不着急，随下次正常发布一起上线 | 1 |

非紧急路径合入 `pre` 后，后续走正常上线流程（`pre` → `main`）。合入 `main` 的热修需在配置时限内完成 `main → pre` 同步，否则触发超时告警。

## 快速开始

**依赖**：Python 3.9+

```bash
# 安装依赖
pip install -r requirements.txt

# 复制配置模板
cp config.yaml.example config.yaml
# 编辑 config.yaml，填写企业微信 webhook、GitLab secret、用户映射

# 启动服务
bash service.sh start
```

服务默认监听 `0.0.0.0:8021`，Webhook 地址：

```
http://<your-server>:<port>/gitlab/webhook
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/gitlab/webhook` | GitLab Webhook 接收 |
| GET | `/violations` | 查询违规记录（用于日报） |
| GET | `/check-hotfix-sync` | 手动触发热修同步超时检查（服务内部会自动定时执行） |

### 违规记录查询

```
GET /violations?start=2026-05-01&end=2026-05-16
```

返回格式：

```json
{
  "total": 2,
  "items": [
    {
      "id": 1,
      "created_at": "2026-05-16 14:14:19",
      "operator": "wujing03",
      "operator_name": "邬晶",
      "violation_type": "direct_push_protected",
      "project": "ai-tool",
      "description": "直接 push 到受保护分支 main，共 3 个提交",
      "detail": "{\"branch\": \"main\", ...}"
    }
  ]
}
```

`start` / `end` 默认为当天，格式 `YYYY-MM-DD`。

### 热修同步超时检查

服务启动后在后台按 `hotfix_sync_check_interval_seconds`（默认 3600 秒）自动循环检查：热修 MR 合入 `main` 后是否在 `hotfix_sync_threshold_hours`（默认 4 小时）内完成了 `main → pre` 同步。超时的项目会收到企微告警并写入 `hotfix_sync_overdue` 违规记录。

`/check-hotfix-sync` 接口用于手动触发（调试或补跑），返回本次告警的记录列表。

## 配置说明

配置优先级：**环境变量 > config.yaml**

| config.yaml 字段 | 对应环境变量 | 说明 |
|-----------------|------------|------|
| `wechat.webhook_url` | `WECHAT_WEBHOOK_URL` | 企业微信群机器人 Webhook URL |
| `gitlab.secret_token` | `GITLAB_WEBHOOK_SECRET` | GitLab Webhook Secret Token（可选） |
| `gitlab.url` | `GITLAB_URL` | GitLab 地址，用于主动查询 API |
| `gitlab.token` | `GITLAB_PRIVATE_TOKEN` | GitLab Personal Access Token，需 `api` 权限 |
| `tl_usernames` | `TL_USERNAMES=user1,user2` | TL / Reviewer 的 GitLab 用户名，违规告警及热修紧急路径额外 @ |
| `hotfix_required_approvals` | `HOTFIX_REQUIRED_APPROVALS` | 热修紧急路径（→ `main`）所需最少 Approve 人数，默认 2；非紧急路径（→ `pre`）固定为 1 |
| `hotfix_sync_threshold_hours` | `HOTFIX_SYNC_THRESHOLD_HOURS` | 热修合入 `main` 后超过此小时数未同步 `pre` 则告警，默认 4 |
| `hotfix_sync_check_interval_seconds` | `HOTFIX_SYNC_CHECK_INTERVAL` | 内部定时检查热修同步状态的间隔秒数，默认 3600（1 小时） |
| `user_map.<username>` | `WECHAT_USER_<username>=<mobile>` | GitLab 用户名 → 企业微信手机号映射 |
| `log.level` | `LOG_LEVEL` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

配置模板见 [config.yaml.example](./config.yaml.example)。

## 服务管理

```bash
bash service.sh start    # 启动
bash service.sh stop     # 停止（SIGTERM 优雅关闭，等待 10s）
bash service.sh restart  # 重启
bash service.sh status   # 查看状态
```

日志位于 `logs/gitlab-hook.log`。

## GitLab 配置

在 GitLab 项目的 **Settings → Webhooks** 中添加：

- **URL**：`http://<your-server>:<port>/gitlab/webhook`
- **Secret token**：与 `gitlab.secret_token` 保持一致（可留空）
- **Trigger**：勾选以下全部 4 类事件

| Webhook 事件 | 覆盖功能 |
|-------------|---------|
| `Issues events` | Issue 创建/重开通知、格式校验（含 assignee）、assignee 变更通知、Issue 关闭违规（无人认领、未产品验收、负责人全部移除） |
| `Merge request events` | MR 创建违规检测（缺 `Closes #xxx`、标题不规范、功能分支直合 main、pre 验收未完成）、MR Approve 通知、MR 合并违规检测（Approve 不足） |
| `Comments` | Issue 评论中 `product:pass/reject`、`developer:pass/reject` 验收口令自动通知；普通评论转发给 assignee |
| `Push events` | 直接 push 受保护分支告警、Force Push 告警 |

## 分支命名约定

| 分支前缀 | 类型 |
|---------|-----|
| `issue_<id>` | 需求/优化功能分支 |
| `hotfix_<id>` | 线上热修分支 |
| `pre` | 预发布分支（上线 MR 源分支） |
| `main` / `master` | 主干 |

## 项目结构

```
app/
├── main.py            # FastAPI 应用入口、请求日志中间件、热修同步后台定时任务
├── webhook.py         # Webhook 路由：校验 Token、分发事件、违规查询、手动触发热修检查
├── handlers.py        # 业务逻辑：Issue / MR / Note / Push 事件处理 + 热修同步检查函数
├── config.py          # 配置加载（config.yaml + 环境变量），初始化 GitLabClient
├── gitlab_client.py   # GitLab API 客户端（Issue / Note / MR 查询）
├── wechat.py          # 企业微信 Webhook 发送
├── state.py           # SQLite 持久化：violations 违规记录 + hotfix_sync_pending 待同步记录
└── logger.py          # 结构化日志
service.sh             # 进程管理脚本（start/stop/restart/status，优雅关闭）
requirements.txt       # Python 依赖
config.yaml.example    # 配置模板
```
