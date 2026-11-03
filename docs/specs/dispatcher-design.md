# Dispatcher

## 本质

Dispatcher 是 FGS 图的**唯一写入者**。它观察图、选出该发生的活动、把活动交给 Agent 进程执行、再把 Agent 的结构化输出写回图。

它不推理任务内容，也不保存任何跨会话记忆：图和配置就是它的全部状态。

## 设计要点

1. **Less is more**：只有两类活动（Decide / Execute），没有固定角色、没有跨会话记忆、没有工具集成层。
2. **外化记忆**：每次活动都从干净上下文重新开始，靠读取 FGS 图恢复状态。
3. **单一写入者**：Agent 只输出结构化 JSON，Server 校验后写图，避免并发写冲突。
4. **提示词极短且与任务领域解耦**：`decide.md` / `execute.md` 只描述图语法与输出契约，不含任何渗透测试术语。
5. **失败不猜测**：契约校验失败、超时、心跳丢失都按失败处理并释放租约，由下一轮重新调度。

## 架构概览

```
┌──────────────┐   HTTP    ┌──────────────┐   exec    ┌──────────────────┐
│ Cairn Server │◀─────────▶│  Dispatcher  │──────────▶│  Worker 容器/进程 │
│   FGS 图     │           │ 调度 + 契约  │           │  Agent CLI       │
└──────────────┘           └──────────────┘           └──────────────────┘
```

- **Cairn Server**：图的读写与一致性（见 `server-protocol.md`）。
- **Dispatcher**：轮询项目摘要、抢占租约、构造提示词、执行 CLI、校验输出、写回图。
- **Worker**：Claude Code / Codex / Pi / Mock；容器模式下每个项目一个容器，本地模式下直接跑在宿主机。

### 执行主链路

1. 定时 tick：`list_projects()`。
2. 回收已完成任务的 Future，更新 Decide checkpoint。
3. 取消非 `active` 项目的运行中任务，清理容器。
4. 选取项目：
   - `active` 且没有运行中任务的项目优先；
   - 受 `max_running_projects` 约束；
   - 空闲项目按 project_cursor 轮转，避免饿死。
5. 项目内选活动：
   - Decide 可抢占且触发条件成立 → 先 Decide；
   - 否则取 `priority` 最高、创建最早的 pending Step → Execute。
6. 抢租约成功 → 提交线程池 → 任务结束后按结果更新 checkpoint 或冷却 worker。

## 配置模型

```yaml
server: "http://cairn-server:8000"
common_env: {}
runtime:
  execution: container          # container | local
  interval: 3
  max_workers: 8
  max_running_projects: 3
  max_project_workers: 4
  healthcheck_timeout: 20
  worker_healthcheck: startup_only   # startup_and_task | startup_only | disabled
  prompt_group: default
tasks:
  decide:
    timeout: 300
    max_steps: 3
  execute:
    timeout: 600
    conclude_timeout: 90
container:
  image: ghcr.io/oritera/cairn-worker-container:latest
  network_mode: host
  completed_action: stop        # stop | remove
  cap_add: []
local:
  workspace_root: null
  completed_action: keep        # keep | remove
workers:
  - name: claudecode-a
    type: claudecode            # claudecode | codex | pi | mock
    task_types: [decide, execute]
    max_running: 2
    priority: 0
    env: {}
```

约束：

- worker 名字唯一；
- `max_project_workers` 不得超过 `max_workers`；
- `task_types` 只能取 `decide` / `execute`；
- 每个 worker 的 env 会和 `common_env` 合并，worker 自身优先。

## 任务模型

### Decide

#### 职责

只读和修改图：评估 Step、添加 / 废弃 / 调整优先级、提出和关闭子 Goal、宣告根 Goal 达成。没有任何作用于世界的工具。

权限边界由契约保证：Decide 的输出即使包含世界操作也会被忽略，Dispatcher 只应用图变更。若需要进程级隔离，可在驱动层进一步限制 CLI 可用工具（`claude` / `codex` / `pi` 各自提供工具白名单），但这不属于协议要求。

#### 触发条件

`_decide_trigger` 命中任一即触发：

| 触发 | 说明 |
|------|------|
| `initial` | 项目从未 Decide 过（没有 checkpoint） |
| `facts:N->M` | 事实增加 |
| `hints:N->M` | 提示增加 |
| `findings:N->M` | Finding 增加 |
| `open_steps:N->0` | 所有 Step 结束，需要重新规划 |

checkpoint 在 Decide 成功后**重新读取图**再写入，这样"Decide 只废弃了 Step"也会在 Step 清空时再次触发。

#### 输入

- `{graph_yaml}`：图快照的容器内路径（大图不直接塞进提示词）。
- `{fact_ids}`：可引用的事实 id 列表。
- `{open_steps}`：pending / running 的 Step 摘要。
- `{max_steps}`：一次运行最多新增多少 Step。

#### 输出契约

```json
{"accepted": true, "data": {
  "complete": {"from": ["f001"], "description": "为什么目标已达成"},
  "goals": {"add": [{"description": "...", "parent": "g001"}],
            "close": [{"id": "g002", "status": "achieved"}]},
  "steps": {"add": [{"description": "...", "from": ["f001"], "goal": "g001",
                     "priority": 3, "rationale": "..."}],
            "update": [{"id": "s002", "priority": 7},
                       {"id": "s003", "status": "abandoned"}]}
}}
```

校验规则：

- 只接受 `steps` / `goals` / `complete` 三个键；也允许 `steps` 直接是数组。
- 所有 fact / goal / step 引用必须存在于图中，否则整次运行失败。
- `complete.from` 非空。
- 新增 Step 数不超过 `max_steps`（超出截断）。
- 没有任何动作且当前没有 open Step → 失败（必须推进）。
- `accepted: false` → 直接判定 rejected。

#### 接口映射

| 契约字段 | 接口 |
|----------|------|
| `complete` | `POST /projects/{id}/complete` |
| `goals.add[]` | `POST /projects/{id}/goals` |
| `goals.close[]` | `POST /projects/{id}/goals/{gid}/close` |
| `steps.add[]` | `POST /projects/{id}/steps` |
| `steps.update[]` | `POST /projects/{id}/steps/{sid}/update` |

#### 超时与失败

超时、命令失败、解析失败、契约失败 → `failed`，释放 Decide 租约，checkpoint 不更新，下一轮重试。无 conclude 兜底（Decide 不作用于世界，重来即可）。

### Execute

#### 触发条件

存在 `pending`、`worker` 为空、且本地没有在跑的 Step。按 `(priority, created_at, id)` 取最大值。

#### 输入

- `{graph_yaml}`：图快照路径。
- `{step_id}` / `{step_description}`：本次唯一的 Step。

工具：read / bash / edit / write 等世界工具，以及 `submit_fact` / `submit_finding`（以输出契约形式提供）。

#### 输出契约

```json
{"accepted": true, "data": {
  "facts": [{"description": "已确认的事实"}],
  "findings": [{"kind": "任务自定义类型", "description": "搜索产物"}],
  "status": "done",
  "note": "可选"
}}
```

校验规则：

- `status` ∈ `done` | `abandoned`。
- `facts` 可以是字符串数组或 `{"description": ...}` 数组；也接受单个 `fact`。
- `status=done` 时至少一条 fact。
- `findings[].kind` 缺省为 `finding`。
- 解析 / 校验失败 → 进入 conclude 兜底。

#### 兜底与失败

| 情况 | 处理 |
|------|------|
| 主阶段正常但输出无法解析 | 用同一 session 追加 `execute_conclude.md`，要求立即停止并汇报 |
| 主阶段超时 | 同上 |
| 兜底也失败 | 释放 Step，回到 pending |
| 心跳冲突（租约被回收） | 立即结束，不写图 |
| 写 conclude 时项目已非 active | 视为正常结束，只释放 Step |
| 连续失败 | worker 进入 rejected 冷却（项目 + 任务类型维度） |

## 调度策略

### 全局

- 每个 tick 先回收 Future、取消非 active 项目任务、处理容器清理。
- 先调度"已有运行中任务"的项目，再调度空闲项目，保证长任务的项目仍然推进。
- 空闲项目通过 `project_cursor` 轮转。
- 运行中项目数受 `max_running_projects` 限制。

### 项目内

1. 若 `max_project_workers` 已满 → 跳过。
2. 若本地已有 Decide 在跑，或租约占不到 → 跳过 Decide。
3. Decide 优先于 Execute：图刚变化时先评估，再执行。
4. Execute 只挑本地没有在跑的 pending Step。

### 并发约束

| 约束 | 来源 |
|------|------|
| 全局并发 | `runtime.max_workers` |
| 项目并发 | `runtime.max_project_workers` |
| 单 worker 并发 | `workers[].max_running` |
| 项目内 Decide 串行 | Server 的 decide 租约 |
| 单 Step 独占 | Server 的 step 租约 |

## Worker

### 字段

| 字段 | 说明 |
|------|------|
| `name` | 唯一名称，出现在租约与日志里 |
| `type` | `claudecode` / `codex` / `pi` / `mock` |
| `task_types` | 该 worker 可承担的活动 |
| `max_running` | 该 worker 并发上限 |
| `priority` | 越小越优先；同优先级看当前负载 |
| `env` | 驱动所需环境变量，与 `common_env` 合并 |

### Driver 接口

```python
prepare_session() -> str | None
build_execute(worker, prompt, session) -> DriverResult
build_conclude(worker, prompt, session) -> list[str]
extract_session(session, stdout, stderr) -> str | None
extract_response_text(stdout, stderr) -> str
supports_conclude() -> bool
check_health(worker, timeout) -> HealthResult
```

`build_execute` 返回 argv 与 session；session 用于把 conclude 兜底续接到同一次对话。

### 健康检查

- `startup_and_task`：启动时检查一次，每个任务前再检查；失败则冷却 worker。
- `startup_only`：只在启动时检查。
- `disabled`：完全跳过自动检查（本地模式默认）。
- 本地模式启动时还会检查 CLI 是否在 PATH 上。

### CLI 接入约定

Agent CLI 只需满足两点：

1. 能从 argv 接收完整提示词；
2. 最终把结构化 JSON 打到 stdout（允许夹杂文本 / markdown 代码块）。

Dispatcher 负责从输出中提取最后一个 JSON 对象。

## 提示词资源

```
cairn/src/cairn/dispatcher/prompts/
  default/decide.md
  default/execute.md
  default/execute_conclude.md
  mock/decide.md
  mock/execute.md
  mock/execute_conclude.md
```

- `prompt_group` 选择目录。
- 加载时校验必需占位符，缺一即启动失败。
- default 组与安全任务解耦：只描述 Fact / Goal / Step 与输出契约。

## Mock 驱动

用于无 LLM 的端到端验证。每个阶段通过环境变量配置行为：

| 环境变量 | 阶段 | 可选结果 |
|----------|------|----------|
| `MOCK_HEALTHCHECK` | 健康检查 | `ok` / `fail` |
| `MOCK_DECIDE` | Decide | `complete` / `steps` / `noop` / `rejected` / `invalid_json` / `invalid_payload` / `command_fail` |
| `MOCK_EXECUTE` | Execute | `facts` / `rejected` / `invalid_json` / `invalid_payload` / `command_fail` |
| `MOCK_EXECUTE_CONCLUDE` | 兜底 | 同上 |

支持按提示词内容设置规则，例如：

```json
{"delay": [0, 0], "outcomes": {"steps": 1},
 "rules": [{"fact_ids_gte": 2, "force": "complete"}]}
```

可用条件：`fact_ids_gte` / `fact_ids_lte` / `open_steps_empty` / `phase`。

## 本地模式

`runtime.execution: local` 时不创建容器，直接在宿主机启动 CLI 进程：

- 每个项目一个工作目录 `<workspace_root>/<project_id>/`；
- 图快照写到宿主机临时目录；
- 复用宿主机已登录的 `claude` / `codex` / `pi`；
- 没有沙箱，只应在有授权的环境使用。
