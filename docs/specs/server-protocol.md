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
pending/running ──Decide abandon──▶ abandoned
```

`kind` 为 `action` 的 Step 是普通动作；`kind` 为 `completion` 的 Step 是完成声明，由 `complete` 接口原子写入。

### Finding

搜索过程的结构化产物。Finding 的定义由任务决定（例如漏洞挖掘任务里就是各类漏洞），所以 `kind` 是自由字符串，不做枚举。Finding 通过 Execute 的 `conclude` 写入，并记录产生它的 Step。

### Hint

人类判断，任何时刻都可以注入，下一次 Decide / Execute 读取图时自然吸收。

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

Step 不会被删除，废弃只改变状态。

#### POST /projects/{project_id}/steps/{step_id}/conclude

Execute 的 `submit_fact` / `submit_finding` 与 Step 收尾，原子完成：

```json
{
  "worker": "executor-a",
  "status": "done",
  "facts": [{"description": "22/tcp open ssh"}],
  "findings": [{"kind": "weak-credential", "description": "admin:admin 可登录"}],
  "note": "可选"
}
```

返回：

```json
{
  "facts": [{"id": "f002", "description": "22/tcp open ssh"}],
  "findings": [{"id": "d001", "kind": "weak-credential", "description": "admin:admin 可登录"}],
  "step": {"id": "s001", "status": "done", "to": "f002"}
}
```

`status` ∈ `done` | `abandoned`。第一条 fact 成为 Step 的 `to`；没有 fact 时 `to` 为 `null`。

### 完成与重开

#### POST /projects/{project_id}/complete

```json
{"from": ["f005"], "description": "已拿到域控", "worker": "decider-a"}
```

原子写入：`kind=completion` 的 Step（来源为 `from`）、根 Goal 置为 `achieved`、项目置为 `completed`。要求 `from` 非空且引用真实事实。

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

写入分四步：跑之前创建（`status: running` + prompt 消息）、跑的过程中追加消息、长时间没有输出时发心跳、结束后用 PATCH 收尾。`running` 会话若 60 秒内没有任何追加或心跳，会被读接口判定为失败（`error` 为 `dispatcher stopped before the run finished`）—— dispatcher 被杀掉时不会留下永远 `running` 的会话。

#### POST /projects/{project_id}/sessions

由 dispatcher 在 worker 启动前调用，建立会话并写入 prompt：

```json
{
  "worker": "decider-a",
  "worker_type": "pi",
  "phase": "decide",
  "step": null,
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

记录的是完整过程，而不是只留最终回答：dispatcher 让每个 worker CLI 以流式 JSON 输出（claudecode `--output-format stream-json --verbose`、codex `exec --json`、pi `--mode json`），再把事件流展开成逐条消息：

- `assistant` / `user`：消息里的 text 块，一段一块。
- `thinking`：`thinking` / `reasoning` 块，或 codex 的 reasoning item。
- `tool_call`：一次工具调用，第一行是工具名，第二行是命令/路径/pattern 等关键参数。
- `tool_result`：工具回填的内容，带 `[exit N]` 前缀；单条上限 8000 字符，其余消息单条上限 20000 字符，单会话最多 1000 条。
- `tool`：整条输出里没有任何可识别事件时的兜底，保留原始 stdout/stderr。

重复事件（pi 的 `turn_end` / `agent_end` 会重放消息、claude 的 `result` 会重复最后一条回答、CLI 回显 prompt）会按内容去重。

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

### 导出

#### GET /projects/{project_id}/export?format=yaml

导出当前 FGS 图快照（`project` / `hints` / `facts` / `goals` / `steps` / `findings`）。

#### GET /projects/{project_id}/export?format=timeline

导出人类可读的时间线，事件包括：PROJECT CREATED、HINT、GOAL DECLARED / ACHIEVED / DROPPED、STEP DECLARED / CONCLUDED / ABANDONED、FINDING、PROJECT COMPLETED。

非法 `format` 返回 `400`。

## 不变量

1. Fact 只追加；`origin` 保留。
2. 根 Goal 有且仅有一个，在项目创建时写入。
3. Step 不删除，只改变状态；唯一的删除是 reopen 撤回 completion Step。
4. 一个 Step 同时只能被一个 worker 持有。
5. 一个项目同时只能有一个 Decide 租约。
6. 项目 `completed` 后所有写接口返回 `403`，只有 reopen 例外。
