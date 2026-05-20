# ai-gitlab-hook

GitLab Webhook 接收服务，将 GitLab 事件转化为企业微信群消息，驱动研发协作工作流中的各个通知节点，同时对违规操作进行实时告警并持久化记录。

**新增功能**：通过 GitLab Commit Status API 实现 **MR 合并门禁**，验收未完成时自动阻止上线 MR 合并。

## 功能概览

### MR 合并门禁（External Status Check）

通过 GitLab Commit Status API 实现多重合并门禁，确保代码质量和流程规范。**所有门禁都无法在 GitLab 页面绕过**，即使不使用 `ccg` 命令也会生效。

#### 1. 上线 MR 验收门禁（`pre` → `main`）

自动检查 pre 分支上**所有已合并 MR** 关联的 Issue 验收状态：
- **检查范围**：遍历 pre 上所有已合并的 MR，提取 `Closes #xxx` 关联的所有 Issue
- **验收要求**：每个 Issue 必须同时完成 `product:pass` 和 `developer:pass`（验收口令必须在 MR 合入 pre 之后发布）
- **验收未完成**：设置 Commit Status 为 `failed`，GitLab 页面无法点击 Merge 按钮
- **验收已完成**：设置 Commit Status 为 `success`，允许合并
- **实时更新**：当 Issue 评论中出现验收口令时，自动更新相关上线 MR 的状态

#### 2. 热修 MR 审批门禁（`hotfix_*`/`hotfix/*` → `main`）

自动检查 Approve 数量：
- **Approve 不足**：设置 Commit Status 为 `failed`，阻止合并（默认需要 2 人）
- **Approve 已满足**：设置 Commit Status 为 `success`，允许合并
- **实时更新**：每次获得新 Approve 时自动更新状态

#### 3. Issue 关联门禁（`issue_*`/`hotfix_*`/`feature/*`/`hotfix/*` → `pre`/`main`）

强制要求所有功能/热修分支的 MR 必须关联 Issue：
- **适用范围**：所有功能分支和热修分支合入 `pre` 或 `main` 时
- **检查内容**：MR 描述必须包含 `Closes #xxx` 引用
- **缺少引用**：设置 Commit Status 为 `failed`，阻止合并
- **已添加引用**：设置 Commit Status 为 `success`，允许合并
- **自动更新**：编辑 MR 描述补充引用后自动解除限制
- **防绕过**：即使功能分支直接合入 main（违规操作），也必须有 Issue 关联才能追踪验收状态

#### 防绕过机制

| 绕过尝试 | 是否被阻止 | 说明 |
|---------|-----------|------|
| 功能分支直接合入 main（跳过 pre） | ✅ 阻止 | Issue 关联门禁生效 + 违规告警 |
| MR 不写 `Closes #xxx` | ✅ 阻止 | Issue 关联门禁对 pre/main 都生效 |
| 验收未完成就上线 | ✅ 阻止 | 上线 MR 验收门禁检查所有 Issue |
| 热修 Approve 不足就合并 | ✅ 阻止 | 热修审批门禁生效 |
| 在 GitLab 页面直接点 Merge | ✅ 阻止 | 所有门禁都通过 Commit Status 实现 |

### 工作流通知

接收 GitLab 推送的四类事件，按业务规则发送企业微信通知并 @ 对应成员：

| 事件 | 触发场景 | 通知对象 |
|------|---------|---------|
| **Issue open / reopen** | 需求/优化 Issue，格式合规（章节齐全且已指派 assignee） | 研发（assignee） |
| **Issue open / reopen** | 需求/优化 Issue，格式不合规（缺少章节或未指派 assignee） | 产品（reporter） |
| **Issue open / reopen** | Bug Issue，格式合规（章节齐全且已指派 assignee） | 研发 + TL |
| **Issue open / reopen** | Bug Issue，格式不合规（缺少章节或未指派 assignee） | 产品（reporter） |
| **Issue update** | 新增 assignee（需求/优化/Bug），模板格式合规 | 新增的研发（assignee） |
| **Issue update** | 新增 assignee（需求/优化/Bug），模板格式不合规（缺少章节） | 产品（reporter） |
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
| 强制推送（Force Push） | `push_force: true`，非 `issue_*`/`hotfix_*` 分支（功能/热修分支 rebase 后的 force-with-lease 属正常操作，不告警） | 操作人 + TL |
| 功能分支直合 main | `issue_*` → `main`/`master` 的 MR，跳过 pre 验收 | 操作人 + TL |
| 上线 MR 验收未完成 | `pre` → `main` MR 创建时实时查 GitLab，有 Issue 在合入 `pre` 之后未完成 `product:pass` + `developer:pass` | 操作人 + TL |
| Issue 无人认领即关闭 | Issue 关闭时 assignees 为空 | TL |
| Issue 负责人被全部移除 | Issue update 时 assignees 由有变无 | TL |
| MR 缺少 Issue 关联 | `issue_*`/`hotfix_*` MR 描述无 `Closes #xxx`，未通过 `ccg mr create` 创建 | 操作人 + TL |
| MR 标题不符规范 | `issue_*` MR 标题不以 `[需求]` 开头，或 `hotfix_*` 不以 `[Bug热修]` 开头 | 操作人 + TL |
| Issue 未验收即关闭 | 需求/优化 Issue 关闭时查 GitLab API，`product:pass` 或 `developer:pass` 任一未完成 | 操作人 + TL |
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

服务启动后在后台按 `hotfix_sync_check_interval_seconds`（默认 60 秒）自动循环检查：热修 MR 合入 `main` 后是否在 `hotfix_sync_threshold_hours`（默认 4 小时）内完成了 `main → pre` 同步。超时的项目会收到企微告警并写入 `hotfix_sync_overdue` 违规记录。

`/check-hotfix-sync` 接口用于手动触发（调试或补跑），返回本次告警的记录列表。

## 配置说明

配置优先级：**环境变量 > config.yaml**

| config.yaml 字段 | 对应环境变量 | 说明 |
|-----------------|------------|------|
| `wechat.webhook_url` | `WECHAT_WEBHOOK_URL` | 企业微信群机器人 Webhook URL |
| `gitlab.secret_token` | `GITLAB_WEBHOOK_SECRET` | GitLab Webhook Secret Token（可选） |
| `gitlab.url` | `GITLAB_URL` | GitLab 地址，用于主动查询 API |
| `gitlab.token` | `GITLAB_PRIVATE_TOKEN` | GitLab Personal Access Token，需 `api` 权限 |
| `main_branch` | `GITLAB_BRANCH_MAIN` | 生产主干分支名，默认 `main`（与 ai-workflow 保持一致） |
| `pre_branch` | `GITLAB_BRANCH_PRE` | 预发布分支名，默认 `pre`（与 ai-workflow 保持一致） |
| `tl_usernames` | `TL_USERNAMES=user1,user2` | TL / Reviewer 的 GitLab 用户名，违规告警及热修紧急路径额外 @ |
| `hotfix_required_approvals` | `HOTFIX_REQUIRED_APPROVALS` | 热修紧急路径（→ `main`）所需最少 Approve 人数，默认 2；非紧急路径（→ `pre`）固定为 1 |
| `hotfix_sync_threshold_hours` | `HOTFIX_SYNC_THRESHOLD_HOURS` | 热修合入 `main` 后超过此小时数未同步 `pre` 则告警，默认 4 |
| `hotfix_sync_check_interval_seconds` | `HOTFIX_SYNC_CHECK_INTERVAL` | 内部定时检查热修同步状态的间隔秒数，默认 60（1 分钟） |
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

### 1. 添加 Webhook

在 GitLab 项目的 **Settings → Webhooks** 中添加：

- **URL**：`http://<your-server>:<port>/gitlab/webhook`
- **Secret token**：与 `gitlab.secret_token` 保持一致（可留空）
- **Trigger**：勾选以下全部 4 类事件

| Webhook 事件 | 覆盖功能 |
|-------------|---------|
| `Issues events` | Issue 创建/重开通知、格式校验（含 assignee）、assignee 变更时重新校验模板并通知、description 补全后通知研发认领、Issue 关闭违规（无人认领、未双方验收、负责人全部移除） |
| `Merge request events` | MR 创建违规检测（缺 `Closes #xxx`、标题不规范、功能分支直合 main、pre 验收未完成）、MR Approve 通知、MR 合并违规检测（Approve 不足）、**MR 合并门禁（Commit Status）** |
| `Comments` | Issue 评论中 `product:pass/reject`、`developer:pass/reject` 验收口令自动通知；普通评论转发给 assignee；**验收状态变化时自动更新 MR Commit Status** |
| `Push events` | 直接 push 受保护分支告警、Force Push 告警（`issue_*`/`hotfix_*` 分支除外） |

### 2. 启用 Merge Checks（必需，用于合并门禁）

在 GitLab 项目的 **Settings → Merge requests → Merge checks** 中：

- ✅ 勾选 **Pipelines must succeed**

这样当 Commit Status 为 `failed` 时，GitLab 会阻止合并操作。

**注意**：
- 如果项目已配置 CI/CD Pipeline，Commit Status 和 Pipeline 状态都必须通过才能合并
- 如果项目没有 Pipeline，只检查 Commit Status
- Commit Status 名称为 `pre-acceptance-check`，会显示在 MR 页面的 "Checks" 区域

### 3. 配置 Protected Branches（推荐）

在 **Settings → Repository → Protected branches** 中：

- 保护 `main` 和 `pre` 分支
- **Allowed to merge**：设置为 `Maintainers` 或更严格的角色
- **Allowed to push**：设置为 `No one`（强制通过 MR 合并）

这样可以防止直接 push 到保护分支，配合 Webhook 的违规检测形成双重保障。

## 合并门禁工作流程

### 1. 上线 MR 验收门禁（pre → main）

```
┌─────────────────────────────────────────────────────────────┐
│ 研发创建上线 MR (pre → main)                                  │
│    ↓                                                         │
│    Webhook 触发 → 检查所有 Issue 验收状态                     │
│    ├─ 未完成 → Commit Status = failed (阻止合并)             │
│    └─ 已完成 → Commit Status = success (允许合并)            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ 产品/研发在 Issue 评论区发布验收口令                          │
│    ↓                                                         │
│    Webhook 触发 → 重新检查验收状态 → 更新 Commit Status      │
│    ├─ 双方都 pass → Status = success (解除阻止)              │
│    └─ 任一方 reject/pending → Status = failed (继续阻止)     │
└─────────────────────────────────────────────────────────────┘
```

### 2. 热修 MR 审批门禁（hotfix_* → main）

```
┌─────────────────────────────────────────────────────────────┐
│ 研发创建热修 MR (hotfix_* → main)                            │
│    ↓                                                         │
│    Webhook 触发 → 检查当前 Approve 数量                       │
│    ├─ 不足 2 人 → Commit Status = failed (阻止合并)          │
│    └─ 已满足 → Commit Status = success (允许合并)            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Reviewer 点击 Approve                                        │
│    ↓                                                         │
│    Webhook 触发 → 重新检查 Approve 数量 → 更新 Commit Status │
│    ├─ 仍不足 → Status = failed (继续阻止)                    │
│    └─ 已满足 → Status = success (解除阻止)                   │
└─────────────────────────────────────────────────────────────┘
```

### 3. Issue 关联门禁（issue_*/hotfix_* → pre/main）

```
┌─────────────────────────────────────────────────────────────┐
│ 研发创建功能/热修 MR (issue_*/hotfix_* → pre/main)          │
│    ↓                                                         │
│    Webhook 触发 → 检查 MR 描述是否包含 Closes #xxx           │
│    ├─ 缺少引用 → Commit Status = failed (阻止合并)           │
│    └─ 已关联 → Commit Status = success (允许合并)            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ 研发编辑 MR 描述，补充 Closes #xxx                            │
│    ↓                                                         │
│    Webhook 触发 → 检测到引用 → 更新 Commit Status            │
│    └─ Status = success (解除阻止)                            │
└─────────────────────────────────────────────────────────────┘
```

#### 3. Issue 关联门禁（功能/热修分支 → pre/main）

**说明**：
- 对 `pre` 和 `main` 分支都生效，防止绕过验收检查
- 功能分支直接合入 `main` 虽然违规，但仍需要 Issue 关联才能追踪验收状态
- 支持新旧分支格式：`issue_*`、`hotfix_*`、`feature/*`、`hotfix/*`

**优势**：
- ✅ 即使不使用 `ccg gitlab mr merge`，也无法绕过这些检查
- ✅ 状态实时同步到 GitLab MR 页面
- ✅ 用户能清楚看到当前阻塞原因
- ✅ 满足条件后自动解除阻止，无需手动操作
- ✅ 强制规范流程，防止遗漏验收或缺少 Issue 关联

## 分支命名约定

| 分支格式 | 类型 | 示例 |
|---------|-----|------|
| `feature/<id>-<desc>` | 需求/优化功能分支 | `feature/123-user-login` |
| `hotfix/<id>-<desc>` | 线上热修分支 | `hotfix/456-payment-fix` |
| `pre` | 预发布分支（上线 MR 源分支） | `pre` |
| `main` / `master` | 主干 | `main` |

**说明**：
- `<id>` 为 Issue ID
- `<desc>` 为简短英文描述，自动从 Issue 标题生成，只包含字母、数字、`-`
- 旧格式 `issue_<id>` 和 `hotfix_<id>` 仍然兼容，但推荐使用新格式

## 项目结构

```
app/
├── main.py            # FastAPI 应用入口、请求日志中间件、热修同步后台定时任务
├── webhook.py         # Webhook 路由：校验 Token、分发事件、违规查询、手动触发热修检查
├── handlers.py        # 业务逻辑：Issue / MR / Note / Push 事件处理 + 热修同步检查函数 + MR 合并门禁
├── config.py          # 配置加载（config.yaml + 环境变量），初始化 GitLabClient
├── gitlab_client.py   # GitLab API 客户端（Issue / Note / MR 查询 + Commit Status 设置）
├── wechat.py          # 企业微信 Webhook 发送
├── state.py           # SQLite 持久化：violations 违规记录 + hotfix_sync_pending 待同步记录
└── logger.py          # 结构化日志
service.sh             # 进程管理脚本（start/stop/restart/status，优雅关闭）
requirements.txt       # Python 依赖
config.yaml.example    # 配置模板
```

## 技术实现细节

### Commit Status API

使用 GitLab 的 [Commit Status API](https://docs.gitlab.com/ee/api/commits.html#post-the-build-status-to-a-commit) 设置 MR 的合并门禁：

```python
POST /api/v4/projects/:id/statuses/:sha
{
  "state": "success",  # pending, running, success, failed, canceled
  "name": "pre-acceptance-check",  # 或 hotfix-approval-check, issue-reference-check
  "description": "所有 Issue 验收已完成"
}
```

### 三种门禁的触发时机

#### 1. 上线 MR 验收门禁（`pre-acceptance-check`）

**触发时机**：
- **MR 创建时**（`merge_request` event, action=`open`）
  - 检查验收状态
  - 设置初始 Commit Status

- **Issue 评论时**（`note` event）
  - 检测到 `product:pass/reject` 或 `developer:pass/reject`
  - 查找相关的开放上线 MR
  - 重新检查验收状态
  - 更新 Commit Status

**状态判断逻辑**：
- **所有 Issue** 的 `product:pass` 和 `developer:pass` 都存在 → `success`
- **任一 Issue** 缺少验收或被 reject → `failed`
- 验收口令必须在 MR 合入 `pre` **之后**发布才有效

#### 2. 热修 MR 审批门禁（`hotfix-approval-check`）

**触发时机**：
- **MR 创建时**（`merge_request` event, action=`open`）
  - 检查当前 Approve 数量
  - 设置初始 Commit Status

- **MR 获得 Approve 时**（`merge_request` event, action=`approved`）
  - 重新查询 Approve 数量
  - 更新 Commit Status

**状态判断逻辑**：
- Approve 数量 >= `hotfix_required_approvals`（默认 2） → `success`
- Approve 数量 < 要求 → `failed`

#### 3. Issue 关联门禁（`issue-reference-check`）

**触发时机**：
- **MR 创建时**（`merge_request` event, action=`open`）
  - 检查 MR description 是否包含 `Closes #xxx`
  - 设置初始 Commit Status

- **MR 描述更新时**（`merge_request` event, action=`update`）
  - 检测 description 变化
  - 重新检查是否包含 `Closes #xxx`
  - 更新 Commit Status

**状态判断逻辑**：
- MR description 包含 `Closes #xxx` → `success`
- 缺少 Issue 引用 → `failed`
- **仅对 `issue_*`/`hotfix_*` → `pre` 的 MR 生效**

### Issue 关联逻辑

上线 MR 通过以下方式关联 Issue：

1. 查询所有已合并到 `pre` 分支的 MR
2. 从每个 MR 的 `description` 中提取 `Closes #xxx` 引用
3. 检查这些 Issue 的验收状态

**为什么需要 Issue 关联门禁？**

如果 MR 缺少 `Closes #xxx` 引用：
- Issue 无法自动关闭
- **上线时无法追踪该 Issue 的验收状态**（会被遗漏）
- 可能导致未验收的功能直接上线

通过在合入 `pre` 时强制要求 `Closes #xxx`，确保所有功能都能被正确追踪和验收。

## 常见问题

### Q: 为什么 MR 页面显示 "Merge blocked: Checks must pass"？

A: 这是合并门禁生效的标志，说明验收未完成。查看 MR 页面的 "Checks" 区域，会显示具体原因（如 "验收未完成：Issue [123, 456] 需要 product:pass 和 developer:pass"）。

### Q: 验收完成后 Merge 按钮还是禁用？

A: 检查以下几点：
1. 确认 Issue 评论中的验收口令格式正确（`product:pass` 和 `developer:pass`）
2. 验收口令必须在 MR 合入 `pre` 之后发布
3. 查看 Webhook 服务日志，确认收到了 Note 事件并成功更新了 Commit Status
4. 刷新 MR 页面，GitLab 可能需要几秒钟同步状态

### Q: 如何临时绕过合并门禁？

A: 不建议绕过，但如果确实需要（如紧急情况）：
1. 在 GitLab 项目设置中临时取消勾选 "Pipelines must succeed"
2. 合并后立即重新勾选
3. 这种操作会被记录在 GitLab 审计日志中

### Q: 合并门禁会影响其他类型的 MR 吗？

A: 不会。合并门禁只对上线 MR（`pre` → `main`）生效，其他 MR（如 `issue_*` → `pre`、`hotfix_*` → `main`）不受影响。


