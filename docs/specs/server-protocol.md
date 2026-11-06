# Cairn 协作探索协议

## 本质

这是一个面向目标的状态空间搜索的显式协作协议。

把渗透测试、代码审计、CTF 这类任务建模为一次搜索后，搜索的全部状态被外化成一张**只追加的 FGS 图**（Fact-Goal-Step）。Server 只负责保证图的一致性，不推理、不调度、不关心任务语义。

### 设计渊源

- **Blackboard Architecture**：所有参与者只读写同一块黑板，不做点对点通信。
- **Stigmergy**：协作通过共享环境的状态变化发生，而不是通过对话。
- **只追加**：事实一旦确认就不再修改；Step / Goal 只改变状态，不删除（唯一例外是 reopen 撤回完成声明）。

## 核心概念

### Fact

已经确认的世界状态。事实只能追加，不能修改或删除。`origin` 是保留 id，表示项目初始状态，也是一个普通事实。

### Goal

项目的完成条件，即搜索的终止条件。Goal 构成一棵树：

- 根 Goal 在创建项目时写入，描述项目目标。
- 子 Goal 可以被 Decide 动态提出或关闭（`achieved` / `abandoned`），用于阶段性推进。
- 根 Goal 只能通过 `POST /projects/{id}/complete` 关闭。

### Step

下一步要做什么，以及如何从已有事实产生新的事实：

- `from` 引用的都是已存在的 Fact id。
- `to` 是它产出的事实 id（执行完成后才有值）。
- `goal` 表示它服务于哪个 Goal。
- `priority` 决定执行顺序，`0`（最低）到 `9`（最高）。
- `rationale` 可选，记录"为什么这一步能推进"。

状态机：

```
pending ──claim──▶ running ──conclude──▶ done
   ▲                  │
   └────release───────┘
pending ──Decide abandon──▶ abandoned
running ──Decide cancel───▶ cancelled
```

`running` 的 Step 由持有它的 worker 收尾（`conclude`），Decide 不能废弃它：中途废弃会丢掉已经跑完的结果（见 `/steps/{id}/update`）。租约过期（`step_timeout`）后 Step 回到 `pending`，此时才可以被废弃。但 Decide 可以**取消** `running` 步骤：该步骤立即转为终态，worker 的下一次心跳与迟到的 `conclude` 都得到 `409`，进程由租约失败路径终止，未提交的结果被丢弃。这是有意的强制终止，不是等待结果。

`kind` 为 `action` 的 Step 是普通动作；`kind` 为 `completion` 的 Step 是完成声明，由 `complete` 接口原子写入。

### Finding

搜索过程的结构化产物。Finding 的定义由任务决定（例如漏洞挖掘任务里就是各类漏洞），所以 `kind` 是自由字符串，不做枚举。Finding 通过 Execute 的 `conclude` 写入，并记录产生它的 Step。

`severity` 是统一的分级，取 `critical` | `high` | `medium` | `low` | `info`（默认 `info`）。不管任务自定义了多少种 `kind`，严重度都用这一套，报告和界面才能跨项目汇总。

### Hint

人类判断，任何时刻都可以注入，下一次 Decide / Execute 读取图时自然吸收。

### 授权边界与运行中干预

图本身只描述「探索到哪一步」，不描述「允许探索到哪里」。以下四类资源是外挂在图上的边界，端点与字段见 `docs/protocol.md` §2.5–2.7：

| 资源 | 层次 | 语义 |
|---|---|---|
| `Constraint` | 提示级 | `allow` / `deny` 规则，渲染在目标之前；Decide 只能追加 `deny` |
| `Steer` | 对话级 | 运行中人类指令 + 主 agent 答复；同时经 pi 扩展在下一次工具调用前注入运行中的 worker |
| `project_scope` | 命令级硬闸门 | host / IP / CIDR / domain 白名单；空范围 = 不启用 |
| 任务级 `tool_rules` | 命令级硬闸门 | 只对该项目生效；平台级 `deny/ask` 不可被项目 `allow` 解除 |

约束与指令**不派生**任何拦截规则：软约束与硬拦截必须分别显式配置，避免提示内容（或其中的注入文字）改写平台策略。Decide 对 `running` 步骤的 `cancelled` 是唯一的「主 agent 强制结束子 agent」动作。

## 数据结构

### Project

```json
{
  "id": "proj_001",
  "title": "internal network",
  "status": "active",
  "created_at": "2026-01-01T00:00:00Z",
  "decide": null
}
```

`status` ∈ `active` | `stopped` | `completed`。

### Project.decide

```json
{
  "worker": "decider-a",
  "trigger": "facts:3->4",
  "started_at": "2026-01-01T00:00:10Z",
  "last_heartbeat_at": "2026-01-01T00:00:20Z"
}
```

Decide 的项目级串行租约。租约过期由 server 自动回收。

### ProjectSummary

```json
{
  "id": "proj_001", "title": "internal network", "status": "active",
  "created_at": "...", "decide": null,
  "fact_count": 4, "goal_count": 2, "open_goal_count": 1,
  "step_count": 5, "running_step_count": 1, "pending_step_count": 2,
  "finding_count": 2, "hint_count": 1
}
```

### Fact

```json
{"id": "f001", "description": "22/tcp open ssh"}
```

### Goal

```json
{
  "id": "g002",
  "parent": "g001",
  "description": "获得任意一个 Web 入口",
  "status": "active",
  "creator": "decider-a",
  "created_at": "2026-01-01T00:00:05Z",
  "closed_at": null
}
```

### Step

```json
{
  "id": "s001",
  "from": ["origin"],
  "to": null,
  "goal": "g001",
  "kind": "action",
  "description": "枚举 10.0.0.1 的开放端口",
  "rationale": "拿到端口列表后才能确定攻击面",
  "priority": 5,
  "status": "pending",
  "creator": "decider-a",
  "worker": null,
  "last_heartbeat_at": null,
  "created_at": "2026-01-01T00:00:06Z",
  "concluded_at": null,
  "note": null
}
```

### ProjectDetail

```json
{
  "project": {},
  "facts": [],
  "goals": [],
  "steps": [],
  "findings": [],
  "hints": []
}
```

### Finding

```json
{
  "id": "d001",
  "kind": "sqli",
  "severity": "high",
  "description": "登录接口存在报错注入",
  "step": "s003",
  "creator": "executor-a",
  "created_at": "2026-01-01T00:01:00Z"
}
```

### Hint

```json
{"id": "h001", "content": "留意 8080", "creator": "human", "created_at": "..."}
```

### Settings

```json
{"step_timeout": 20, "decide_timeout": 20}
```

两个超时都以秒为单位，用于回收失效的执行租约。

## 接口列表

所有写接口在项目不处于 `active` 时返回 `403`（hint 写入与 reopen 除外）。引用了不存在的 fact / goal / step 返回 `404`，状态冲突返回 `409`。

`GET /projects` 与 `GET /projects/{id}` 返回 `ETag` 与 `Cache-Control: no-cache`。请求带上 `If-None-Match`（浏览器在轮询时自动携带）且内容未变化时返回 `304`，不传输响应体；图或项目数据发生变化后 tag 随之改变，返回完整的 `200`。

### 健康检查

#### GET /healthz

供进程管理与容器探针使用。数据库不可达时返回 `503`：

```json
{
  "status": "ok",
  "version": "0.2.1",
  "database": "ok",
  "schema_version": 1
}
```

### 全局设置

#### GET /settings

```json
{"step_timeout": 20, "decide_timeout": 20}
```

#### PUT /settings

```json
{"step_timeout": 30, "decide_timeout": 45}
```

要求两个值都 ≥ 5。

### Projects

#### GET /projects

返回 `ProjectSummary[]`，按创建时间升序。

#### POST /projects

```json
{
  "title": "internal network",
  "origin": "目标 10.0.0.1",
  "goal": "拿到域控权限",
  "hints": [{"content": "先看 8080", "creator": "human"}]
}
```

原子写入：项目、`origin` 事实、根 Goal（`g001`）与若干 Hint。返回 `ProjectDetail`。

#### GET /projects/{project_id}

返回 `ProjectDetail`。读取前会回收过期的 step / decide 租约。

#### DELETE /projects/{project_id}

级联删除图。

#### PUT /projects/{project_id}/title

```json
{"title": "new title"}
```

#### PUT /projects/{project_id}/status

```json
{"status": "stopped"}
```

`active` ↔ `stopped`。转 `stopped` 时释放所有 running Step 与 Decide 租约。`completed` 项目不可改状态。

#### POST /projects/{project_id}/decide/claim

```json
{"worker": "decider-a", "trigger": "facts:3->4"}
```

抢占项目级 Decide 租约；已被其他 worker 持有时返回 `409`。同一 worker 重复 claim 幂等成功。

#### POST /projects/{project_id}/decide/heartbeat

```json
{"worker": "decider-a"}
```

续租；租约不属于该 worker 时返回 `409`。

#### POST /projects/{project_id}/decide/release

```json
{"worker": "decider-a"}
```

释放租约，幂等。

### Hints

#### POST /projects/{project_id}/hints

```json
{"content": "人类补充", "creator": "human"}
```

项目处于 `stopped` 时仍可写入。

### Goals

#### POST /projects/{project_id}/goals

```json
{"description": "获得任意一个 Web 入口", "parent": "g001", "creator": "decider-a"}
```

省略 `parent` 时挂到根 Goal 下。

#### POST /projects/{project_id}/goals/{goal_id}/close

```json
{"status": "achieved", "worker": "decider-a", "note": "已拿到 shell"}
```

`status` ∈ `achieved` | `abandoned`。根 Goal 必须通过 `complete` 关闭，直接关闭返回 `409`。

### Steps

#### POST /projects/{project_id}/steps

```json
{
  "from": ["f001"],
  "goal": "g001",
  "description": "枚举端口",
  "rationale": "确定攻击面",
  "priority": 5,
  "creator": "decider-a",
  "worker": null
}
```

`creator` 必须等于 `worker` 或 `worker` 为 `null`，否则 `400`。`from` 为空表示从零开始的一步。

#### POST /projects/{project_id}/steps/{step_id}/heartbeat

```json
{"worker": "executor-a"}
```

抢占 / 续租一个 Step。已结束（`done` / `abandoned`）返回 `409`，被其他 worker 持有也返回 `409`。

#### POST /projects/{project_id}/steps/{step_id}/release

```json
{"worker": "executor-a"}
```

把 Step 放回 `pending`，幂等。

#### POST /projects/{project_id}/steps/{step_id}/update

Decide 专用：调整优先级或废弃。

```json
{"priority": 9}
{"status": "abandoned", "note": "方向已被事实否定"}
```

Step 不会被删除，废弃只改变状态。`running` 的 Step 返回 `409`：它归执行它的 worker 所有，必须由该 worker `conclude` 收尾，否则跑完的结果会被丢弃、Step 也拿不到事实。

#### POST /projects/{project_id}/steps/{step_id}/conclude

Execute 的 `submit_fact` / `submit_finding` 与 Step 收尾，原子完成：

```json
{
  "worker": "executor-a",
  "status": "done",
  "facts": [{"description": "22/tcp open ssh"}],
  "findings": [{"kind": "weak-credential", "severity": "high", "description": "admin:admin 可登录"}],
  "note": "可选"
}
```

`severity` 可省略，缺省为 `info`；大小写与首尾空白会被归一化，未知取值返回 `422`。

返回：

```json
{
  "facts": [{"id": "f002", "description": "22/tcp open ssh"}],
  "findings": [{"id": "d001", "kind": "weak-credential", "severity": "high", "description": "admin:admin 可登录"}],
  "step": {"id": "s001", "status": "done", "to": "f002"}
}
```

`status` ∈ `done` | `abandoned`。第一条 fact 成为 Step 的 `to`；没有 fact 时 `to` 为 `null`。

### 完成与重开

#### POST /projects/{project_id}/complete

```json
{"from": ["f005"], "description": "已拿到域控", "worker": "decider-a"}
```

原子写入：`kind=completion` 的 Step（来源为 `from`）、根 Goal 置为 `achieved`、项目置为 `completed`。要求 `from` 非空且引用真实事实；还有任何 `pending` / `running` 的 `action` Step 或未关闭的子 Goal 时返回 `409`，先收尾再完成。

#### POST /projects/{project_id}/reopen

```json
{"description": "外部复核发现该 shell 属于沙箱", "creator": "human"}
```

仅对 `completed` 项目可用。撤回完成声明：删除 completion Step，根 Goal 回到 `active`，外部反馈作为新事实写入，项目回到 `active`。新的 Decide 会因图变化被触发。

### Sessions（对话记录）

Session 记录一次 worker 运行中模型看到的输入和它的输出，用于事后回看"模型到底看到了什么、回了什么"。它不参与图的推理，也不影响调度，只追加。

- `id` 在项目内自增（`x001`、`x002`…）。
- `phase` ∈ `decide` | `execute` | `execute_conclude`，表示这次运行属于哪个阶段。
- `step` 仅 Execute 阶段有值。
- `status` ∈ `running` | `success` | `failed` | `cancelled` | `rejected`；`running` 表示 worker 还在跑，消息仍在追加。
- `message_count` 是该会话已有的消息数，列表接口也会带上，便于显示"增长中"的进度。
- `summary` 是一行"这次运行要做什么"：Execute 取 Step 的标题（没有标题时取描述），Decide 取待办步骤数，例如 `决策下一步（3 个待办步骤）`。会话列表不用展开 transcript 就能分辨每次运行。创建时写入，最多 300 字符；旧会话没有这个字段，读作空串。
- 用量字段：`input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` / `total_tokens`。由 dispatcher 从 worker CLI 的输出里解析，创建与收尾时都接受；CLI 不报用量时全部为 0，绝不估算。`total_tokens` 取各分量之和与 CLI 自报总量的较大值。
- 四个分量互不重叠：`cache_read_tokens` 是独立的缓存命中量。OpenAI 风格接口把缓存命中算进 `input_tokens`，dispatcher 会先减掉再上报，避免项目总量重复计数。

写入分四步：跑之前创建（`status: running` + prompt 消息）、跑的过程中追加消息、长时间没有输出时发心跳、结束后用 PATCH 收尾。`running` 会话若 60 秒内没有任何追加或心跳，会被读接口判定为失败（`error` 为 `dispatcher stopped before the run finished`）—— dispatcher 被杀掉时不会留下永远 `running` 的会话。

#### POST /projects/{project_id}/sessions

由 dispatcher 在 worker 启动前调用，建立会话并写入 prompt：

```json
{
  "worker": "decider-a",
  "worker_type": "pi",
  "phase": "decide",
  "step": null,
  "summary": "决策下一步（3 个待办步骤）",
  "native_session": "可选，worker CLI 自己的 session id",
  "status": "running",
  "messages": [
    {"role": "user", "content": "发给模型的 prompt"}
  ]
}
```

也接受一次性的完整写入（`status` 为终态并带上全部 `messages`），供创建失败时的兜底使用。`status` 必填——漏传会被拒（`422`），避免默默记成错误的状态。返回 201 与该 Session 详情（`messages` 带 `seq`）。

#### POST /projects/{project_id}/sessions/{session_id}/messages

运行过程中追加消息，dispatcher 约每秒推送一次新条目：

```json
{
  "messages": [
    {"role": "assistant", "content": "先看看目录"},
    {"role": "tool_call", "content": "Bash\nls -la"},
    {"role": "tool_result", "content": "[exit 0]\na.txt"}
  ]
}
```

`seq` 由服务端接着已有的最大值继续分配（应答里带回完整会话）。因此会话页可以在运行中持续刷新，看到思考、调用、结果的完整过程。只有 `running` 状态的会话接受追加；已收尾的会话返回 `409`，dispatcher 收到 `409` 会丢弃该批消息（重试不可能成功）。

#### POST /projects/{project_id}/sessions/{session_id}/heartbeat

worker 长时间没有输出（例如跑一个很慢的命令）时，dispatcher 每 5 秒调用一次，刷新 `updated_at`，避免会话被判定为卡死。无请求体，返回当前 Session。对已收尾的会话是空操作：不会刷新 `updated_at`，`updated_at` 始终保持真实的结束时间。

#### PATCH /projects/{project_id}/sessions/{session_id}

运行结束后收尾，写入真实结果：

```json
{
  "status": "success",
  "returncode": 0,
  "timed_out": false,
  "duration_ms": 8421,
  "error": null,
  "native_session": "可选，回填 CLI 自己的 session id"
}
```

`role` ∈ `system` | `user` | `assistant` | `thinking` | `tool_call` | `tool_result` | `tool`。写入前 dispatcher 会遮蔽 `sk-*`、`Bearer *`、`*_API_KEY=*` 一类的凭据。

记录的是完整过程，而不是只留最终回答：dispatcher 让 worker CLI 以流式 JSON 输出（`pi --mode json`），再把事件流展开成逐条消息：

- `assistant` / `user`：消息里的 text 块，一段一块。
- `thinking`：`thinking` 块。
- `tool_call`：一次工具调用，第一行是工具名，第二行是命令/路径/pattern 等关键参数。
- `tool_result`：工具回填的内容，带 `[exit N]` 前缀；单条上限 8000 字符，其余消息单条上限 20000 字符，单会话最多 1000 条。
- `tool`：整条输出里没有任何可识别事件时的兜底，保留原始 stdout/stderr。

重复事件（`turn_end` / `agent_end` 会重放消息、CLI 回显 prompt）会按内容去重。

#### GET /projects/{project_id}/sessions

按时间顺序返回该项目的全部 Session 元数据（不含 `messages`）。

#### GET /projects/{project_id}/sessions/{session_id}

返回单个 Session 及其 `messages`。前端会话页通过下面的 `/stream` 接口实时刷新；只在 SSE 不可用时才退化为轮询这个接口。

Session 内容属于旁路信息，不属于 FGS 图，因此导出 YAML / timeline 不包含它。Session 随项目一起删除；`GET /projects/{project_id}` 的响应也会带上 `sessions` 列表。

#### GET /projects/{project_id}/sessions/{session_id}/stream

SSE（`text/event-stream`）对话流，会话页用它实时看到消息增长，连接期间不用轮询：

- `snapshot`：连接后的第一个事件，带完整 `session` 与全部 `messages`，中途连上也能直接渲染；同一条连接只发一次。
- `messages`：只带连接建立之后新增的 `messages`（同时带最新 `session` 元数据）。
- `end`：会话进入终态，或 300 秒没有任何新增（说明写入端已消失）时发出，带最终 `session`，随后服务端关闭连接。
- `gone`：会话被删除时发出。
- 其余时间每约 0.5 秒发一行 `: keep-alive` 注释。

响应带 `Cache-Control: no-cache, no-transform` 与 `X-Accel-Buffering: no`，nginx 代理侧也必须关闭缓冲（本仓库的 [nginx 配置](../../deploy/nginx/default.conf.template) 已设置 `proxy_buffering off`），否则消息会被攒住不刷新。

### Questions（旁路提问）

旁路提问是一条只读通道：人类在任务进行中问一句图外的问题，dispatcher 用一个空闲 worker 基于当前图快照作答。答案只写进 `questions` 表，不产生 fact / step / hint，也不占用 Decide / Execute 的并发槽位。

```json
{
  "id": "q001",
  "project_id": "proj_001",
  "content": "这个 CVE 具体是什么？",
  "status": "pending",
  "answer": null,
  "error": null,
  "worker": null,
  "created_at": "2026-01-01T00:05:00Z",
  "started_at": null,
  "answered_at": null,
  "duration_ms": null
}
```

- `id` 在项目内自增（`q001`、`q002`…）。
- `status` ∈ `pending` | `running` | `answered` | `failed` | `cancelled`；`pending` 是还没被 dispatcher 抢到，`running` 是已有 worker 接手。
- `content` 去空白后长度 1–4000，全空白返回 `422`。
- `pending` 超过 300 秒、`running` 超过 900 秒没有进展的，会被读接口判定为 `failed`，避免界面永远转圈。

#### GET /projects/{project_id}/questions

按时间顺序返回该项目的全部提问。

#### POST /projects/{project_id}/questions

```json
{"content": "这个 CVE 具体是什么？"}
```

返回 201 与该 Question。`completed` 项目不能再提问（worker 容器已经不在），返回 `403`。

#### POST /projects/{project_id}/questions/{question_id}/cancel

把 `pending` / `running` 的问题置为 `cancelled` 并写入 `answered_at`；已到终态返回 `409`。正在跑的 worker 不受影响，它稍后提交的答案会被丢弃。

#### DELETE /projects/{project_id}/questions

清空历史，只删除终态（`answered` / `failed` / `cancelled`）记录，返回 `{"deleted": N}`；仍在等待的问题保留。

#### POST /projects/{project_id}/questions/{question_id}/claim

内部接口，dispatcher 调用 worker 之前抢占：

```json
{"worker": "pi-main"}
```

`pending` → `running`，记录 `worker` 与 `started_at`。已被抢占或已不是 `pending` 时返回 `409`。

#### POST /projects/{project_id}/questions/{question_id}/answer

内部接口，worker 跑完后提交结果：

```json
{"status": "answered", "answer": "CVE-2024-1 是演示用的占位编号", "worker": "pi-main", "duration_ms": 4210}
{"status": "failed", "error": "worker timed out", "worker": "pi-main"}
```

`status` ∈ `answered` | `failed`；`answered` 必须带 `answer`，`failed` 必须带 `error`，缺了返回 `422`。只有 `running` 的问题能作答（必须先 claim），重复提交返回 `409`。

`worker` 必须与 claim 时记录的 worker 一致：答案只认领主的提交，其他 dispatcher（或漏传 `worker`）一律返回 `409`，避免多 dispatcher 场景下答案被覆盖。

#### GET /questions/pending

内部接口，跨项目返回最早的待答问题供 dispatcher 领取：默认 5 条，`limit` 取值 1–50，越界返回 `422`。

#### GET /projects/{project_id}/questions/stream

SSE（`text/event-stream`）提问流：

- `snapshot`：连接后的第一个事件，带完整 `questions` 列表。
- `questions`：列表变化时发出，仍带完整列表（列表很短，不做增量）。
- `end`：没有任何 `pending` / `running` 的问题，或连接超过 900 秒时发出，随后关闭连接。
- `gone`：项目被删除时发出。
- 其余时间每秒发一行 `: keep-alive` 注释。

### Context（滚动图摘要）

图快照超出 worker 的上下文预算时，dispatcher 把最近窗口之外的历史压成摘要，连同"覆盖了多少条"一起存到项目上，后续 Decide / Execute / 提问直接复用，不再重复调用模型。三条通道各自独立：

| 通道 | 字段 | 内容 |
| --- | --- | --- |
| 事实 | `summary` / `facts_covered` | 已确认的结论、凭据、拓扑 |
| 已关闭 Step | `step_summary` / `steps_covered` | 试过什么、为什么没成 |
| Finding | `finding_summary` / `findings_covered` | 版本、服务、拓扑等持久观察 |

```json
{
  "summary": "- 10.0.0.5 上的数据库可访问 (f012)",
  "facts_covered": 75,
  "step_summary": "- sqlmap 打 /login 没有注入点 (s003)",
  "steps_covered": 42,
  "finding_summary": "- 22/tcp openssh 8.2 (d001)",
  "findings_covered": 8
}
```

#### GET /projects/{project_id}/context

返回三条通道的当前摘要；从未生成过的通道是空串 + 0。

#### PUT /projects/{project_id}/context

```json
{"summary": "- 10.0.0.5 上的数据库可访问 (f012)", "facts_covered": 75, "steps_covered": 42}
```

按通道合并写入并返回合并后的完整摘要：**每个通道只向前推进**——覆盖数小于已存值、或文本为空时保留旧值。摘要由后台任务写入，这个规则保证晚到的旧摘要不会覆盖新摘要。摘要属于旁路信息，不进入导出 YAML / timeline。

### Findings（汇总）

#### GET /projects/{project_id}/findings

按创建时间返回全部 Finding。

#### GET /projects/{project_id}/findings/summary

返回已确认 Finding 的聚合，用于报告和界面徽标：

```json
{
  "project_id": "proj_001",
  "total": 3,
  "by_severity": {"critical": 1, "high": 2},
  "by_kind": {"sqli": 1, "weak-credential": 2}
}
```

`by_severity` 按严重度从高到低排列，只包含出现过的等级；需要逐条内容时用 `GET /projects/{project_id}/findings`。

### Usage（用量）

#### GET /projects/{project_id}/usage

汇总项目内所有 Session 的实测 token 用量，并给出按阶段、按 worker 的拆分：

```json
{
  "project_id": "proj_001",
  "sessions": 12,
  "input_tokens": 184000,
  "output_tokens": 21000,
  "cache_read_tokens": 96000,
  "cache_write_tokens": 12000,
  "total_tokens": 313000,
  "by_phase": {"decide": {"sessions": 5, "total_tokens": 121000}},
  "by_worker": {"decider-a": {"sessions": 5, "total_tokens": 121000}}
}
```

未上报用量的 Session 计 0，因此总量永远只由实测值组成。

### 导出

#### GET /projects/{project_id}/export?format=yaml

导出当前 FGS 图快照（`project` / `hints` / `constraints` / `steers` / `scope` / `facts` / `goals` / `steps` / `findings`），Finding 带 `severity`。`steers` 含 `answer` / `answered_at`。

#### GET /projects/{project_id}/export?format=timeline

导出人类可读的时间线，事件包括：PROJECT CREATED、HINT、CONSTRAINT REQUIRED / FORBIDDEN、STEER / STEER ANSWER、SCOPE ENTRY、GOAL DECLARED / ACHIEVED / DROPPED、STEP DECLARED / CONCLUDED / ABANDONED / CANCELLED、FINDING、PROJECT COMPLETED。FINDING 行会带上 `severity`。

#### GET /projects/{project_id}/export?format=findings

导出 Markdown 发现报告：按严重度分组，组内逐条列出 `id`、`kind`、产生它的 Step 与描述。

非法 `format` 返回 `400`。

## 不变量

1. Fact 只追加；`origin` 保留。
2. 根 Goal 有且仅有一个，在项目创建时写入。
3. Step 不删除，只改变状态；唯一的删除是 reopen 撤回 completion Step。
4. 一个 Step 同时只能被一个 worker 持有。
5. 一个项目同时只能有一个 Decide 租约。
6. 项目 `completed` 后所有写接口返回 `403`，只有 reopen 例外。
7. 旁路提问只写 `questions` 表：不新增事实、不占用 Step，答案不进入图。
