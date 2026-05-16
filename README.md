# ai-gitlab-hook

GitLab Webhook 接收服务，将 GitLab 事件转化为企业微信群消息，驱动研发协作工作流中的各个通知节点。

## 功能概览

接收 GitLab 推送的三类事件，按业务规则发送企业微信通知并 @ 对应成员：

| 事件 | 触发场景 | 通知对象 |
|------|---------|---------|
| **Issue open** | 新建`【需求】`Issue，格式合规 | 研发（assignee） |
| **Issue open** | 新建`【需求】`Issue，格式不合规 | 产品（reporter） |
| **Issue open** | 新建`【Bug】`Issue，格式合规 | 研发 + TL |
| **Issue open** | 新建`【Bug】`Issue，格式不合规 | 产品（reporter） |
| **Issue update** | 不合规 Issue 格式补全后 | 研发（assignee） |
| **MR approved** | 需求 MR（`issue_*` 分支）通过 | 研发 |
| **MR approved** | 热修 MR（`hotfix_*` 分支）通过 | 研发 + TL |
| **MR approved** | 上线 MR（`pre` 分支）通过 | 研发 |
| **MR approved** | 热修同步 MR（`main/master → pre`）通过 | 研发 |
| **Note** | Issue 评论含 `product:pass/reject` | 研发 |
| **Note** | Issue 评论含 `developer:pass/reject` | 产品 |
| **Note** | 普通 Issue 评论 | 研发（排除评论人自己） |

### Issue 格式校验

**需求 Issue** 必须包含以下二级标题：`需求背景`、`功能详细描述`、`验收标准`、`优先级`

**Bug Issue** 必须包含以下二级标题：`问题现象`、`复现步骤`、`预期正常结果`、`实际异常结果`、`出现环境`、`严重等级`

格式不合规时通知产品补充；补全后自动触发研发认领通知。

## 快速开始

**依赖**：Python 3.11+

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

健康检查：

```
GET /health
```

## 配置说明

配置优先级：**环境变量 > config.yaml**

| config.yaml 字段 | 对应环境变量 | 说明 |
|-----------------|------------|------|
| `wechat.webhook_url` | `WECHAT_WEBHOOK_URL` | 企业微信群机器人 Webhook URL |
| `gitlab.secret_token` | `GITLAB_WEBHOOK_SECRET` | GitLab Webhook Secret Token（可选） |
| `tl_usernames` | `TL_USERNAMES=user1,user2` | TL / Reviewer 的 GitLab 用户名，热修场景额外 @ |
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
- **Trigger**：勾选 `Issues events`、`Merge request events`、`Comments`

## 分支命名约定

服务根据 MR 的源分支名判断 MR 类型：

| 分支前缀 | 类型 |
|---------|-----|
| `issue_<id>` | 需求功能分支 |
| `hotfix_<id>` | 线上热修分支 |
| `pre` | 预发布分支（上线 MR 源分支） |
| `main` / `master` | 主干（热修同步到 pre 时的源分支） |

## 项目结构

```
app/
├── main.py       # FastAPI 应用入口及请求日志中间件
├── webhook.py    # Webhook 路由，校验 GitLab Secret
├── handlers.py   # 业务处理：Issue / MR / Note 事件
├── config.py     # 配置加载（config.yaml + 环境变量）
├── wechat.py     # 企业微信 Webhook 发送
├── state.py      # 轻量状态机（SQLite，追踪不合规 Issue）
└── logger.py     # 结构化日志
service.sh        # 进程管理脚本（start/stop/restart/status）
requirements.txt  # Python 依赖
config.yaml.example  # 配置模板
```
