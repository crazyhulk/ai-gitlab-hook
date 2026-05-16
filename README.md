# ai-gitlab-hook

GitLab Webhook 接收服务，将 GitLab 事件转化为企业微信群消息，驱动研发协作工作流中的各个通知节点，同时对违规操作进行实时告警并持久化记录。

## 功能概览

### 工作流通知

接收 GitLab 推送的四类事件，按业务规则发送企业微信通知并 @ 对应成员：

| 事件 | 触发场景 | 通知对象 |
|------|---------|---------|
| **Issue open / reopen** | 需求/优化 Issue，格式合规 | 研发（assignee） |
| **Issue open / reopen** | 需求/优化 Issue，格式不合规 | 产品（reporter） |
| **Issue open / reopen** | Bug Issue，格式合规 | 研发 + TL |
| **Issue open / reopen** | Bug Issue，格式不合规 | 产品（reporter） |
| **Issue update** | description 变更前不合规、变更后合规 | 研发（assignee） |
| **MR approved** | 需求 MR（`issue_*` 分支）通过 | 研发 |
| **MR approved** | 热修 MR（`hotfix_*` 分支）通过 | 研发 + TL |
| **MR approved** | 上线 MR（`pre` 分支）通过 | 研发 |
| **MR approved** | 热修同步 MR（`main/master → pre`）通过 | 研发 |
| **Note** | Issue 评论含 `product:pass/reject` | 研发 |
| **Note** | Issue 评论含 `developer:pass/reject` | 产品 |
| **Note** | 普通 Issue 评论 | 研发（排除评论人自己） |
| **Push** | 直接 push 到受保护分支 | 研发 + TL |

### Issue 类型识别

Issue 类型根据 description 中的模板章节自动识别，无需标题前缀：

| 类型 | 识别依据（唯一章节） | 必填章节 |
|------|-------------------|---------|
| 需求 | `## 需求背景` | 需求背景、功能详细描述、验收标准、优先级 |
| 优化 | `## 优化背景` | 优化背景、现状问题、优化方案、预期收益、优先级 |
| Bug  | `## 问题现象` | 问题现象、复现步骤、预期正常结果、实际异常结果、出现环境、严重等级 |

格式不合规时通知产品补充；补全后自动触发研发认领通知。

### 违规操作告警

检测以下违规行为，实时推送企业微信告警，同时写入 SQLite 持久化记录：

| 违规类型 | 触发条件 | 通知对象 |
|---------|---------|---------|
| 直接 push 受保护分支 | push 到 `main`/`master`/`pre` | 操作人 + TL |
| 强制推送（Force Push） | `push_force: true`，任意分支 | 操作人 + TL |
| 功能分支直合 main | `issue_*` → `main`/`master` 的 MR | 操作人 + TL |
| 热修目标分支错误 | `hotfix_*` → `pre` 的 MR | 操作人 + TL |
| 上线 MR 验收未完成 | `pre` → `main` MR 创建时实时查 GitLab，有 Issue 未完成 `product:pass` + `developer:pass` | 操作人 + TL |
| Issue 无人认领即关闭 | Issue 关闭时 assignees 为空 | TL |

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

## 配置说明

配置优先级：**环境变量 > config.yaml**

| config.yaml 字段 | 对应环境变量 | 说明 |
|-----------------|------------|------|
| `wechat.webhook_url` | `WECHAT_WEBHOOK_URL` | 企业微信群机器人 Webhook URL |
| `gitlab.secret_token` | `GITLAB_WEBHOOK_SECRET` | GitLab Webhook Secret Token（可选） |
| `gitlab.url` | `GITLAB_URL` | GitLab 地址，用于主动查询 API（如上线验收检查） |
| `gitlab.token` | `GITLAB_PRIVATE_TOKEN` | GitLab Personal Access Token，需 `api` 权限 |
| `tl_usernames` | `TL_USERNAMES=user1,user2` | TL / Reviewer 的 GitLab 用户名，违规告警及热修场景额外 @ |
| `hotfix_required_approvals` | `HOTFIX_REQUIRED_APPROVALS` | 热修 MR 所需最少 Approve 人数，默认 2 |
| `user_map.<username>` | `WECHAT_USER_<username>=<mobile>` | GitLab 用户名 → 企业微信手机号映射 |
| `log.level` | `LOG_LEVEL` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

配置模板见 [config.yaml.example](./config.yaml.example)。

## 服务管理

```bash
bash service.sh start    # 启动
bash service.sh stop     # 停止
bash service.sh restart  # 重启
bash service.sh status   # 查看状态
```

日志位于 `logs/gitlab-hook.log`。

## GitLab 配置

在 GitLab 项目的 **Settings → Webhooks** 中添加：

- **URL**：`http://<your-server>:<port>/gitlab/webhook`
- **Secret token**：与 `gitlab.secret_token` 保持一致（可留空）
- **Trigger**：勾选 `Issues events`、`Merge request events`、`Comments`、`Push events`

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
├── main.py            # FastAPI 应用入口及请求日志中间件
├── webhook.py         # Webhook 路由，校验 GitLab Secret，提供违规查询接口
├── handlers.py        # 业务处理：Issue / MR / Note / Push 事件
├── config.py          # 配置加载（config.yaml + 环境变量），初始化 GitLabClient
├── gitlab_client.py   # GitLab API 客户端（Issue / Note / MR 查询）
├── wechat.py          # 企业微信 Webhook 发送
├── state.py           # SQLite 持久化：违规记录读写
└── logger.py          # 结构化日志
service.sh             # 进程管理脚本（start/stop/restart/status）
requirements.txt       # Python 依赖
config.yaml.example    # 配置模板
```
