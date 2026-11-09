# Dispatcher

## 本质

Dispatcher 是 FGS 图的**唯一写入者**。它观察图、选出该发生的活动、把活动交给 Agent 进程执行、再把 Agent 的结构化输出写回图。

它不推理任务内容，也不保存任何跨会话记忆：图和配置就是它的全部状态。

## 设计要点

1. **Less is more**：调度活动只有两类（Decide / Execute）；旁路提问（Question）与背景摘要（Summary）走独立额度，不抢这两类槽位。没有固定角色、没有跨会话记忆、没有工具集成层。
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
- **Worker**：Pi / Mock；容器模式下每个项目一个容器，本地模式下直接跑在宿主机。

### 模块结构

`DispatcherLoop` 由单文件拆分为一组聚焦的 mixin，公共状态与跨 mixin 接口声明在
`_base.py`（`LoopBase`），组合入口仍保持 `cairn.dispatcher.scheduler.loop.DispatcherLoop` 不变：

```
dispatcher/scheduler/
  worker_select.py       按 priority / 当前负载挑选 worker
  loop/
    __init__.py          组合 DispatcherLoop，再导出 time / shutil 供测试 patch
    _base.py             LoopBase：共享属性 + 跨 mixin 方法签名
    _common.py           退避常量（DECIDE/UNHEALTHY/REJECTED）、预算常量、WorkerSelection
    core.py              CoreMixin：__init__ / close / run 主循环 / 启动健康检查 / 设置校验
    dispatch.py          DispatchMixin：全局与项目级派发入口
    dispatch_decide.py   DecideDispatchMixin：Decide 触发判定与提交
    dispatch_execute.py  ExecuteDispatchMixin：Execute 提交与 Step 尝试计数
    scheduling.py        SchedulingMixin：worker 选择与在跑任务统计
    questions.py         QuestionsMixin：旁路提问与背景摘要
    reaper.py            ReaperMixin：Future 回收、退避记录、容器清理
    checkpoints.py       CheckpointsMixin：Decide checkpoint 与项目运行集
    log_state.py         LogStateMixin：抑制重复日志
```

新增 mixin 需要同时把状态或跨 mixin 方法补进 `_base.py`，否则 `mypy` 会因
`attr-defined` 失败——这条约束替代了原先整包关闭 `attr-defined` 的妥协。

### 执行主链路

1. 定时 tick：`run_startup_healthchecks()`（仅首次），随后每轮先做一次设置校验（仅首次）。
2. `list_projects()` 拉取项目摘要。
3. 回收已完成任务的 Future，更新 Decide checkpoint 与失败退避。
4. 取消非 `active` 项目的运行中任务，排队清理已结束项目的容器。
5. 回收摘要 Future、派发待答旁路提问（各自独立线程池）。
6. 选取项目：
   - `active` 且没有运行中任务的项目优先；
   - 受 `max_running_projects` 约束；
   - 空闲项目按 project_cursor 轮转，避免饿死。
7. 项目内选活动：
   - Decide 可抢占且触发条件成立 → 先 Decide；
   - 否则按 `priority` 从高到低填充 `max_running_steps` 个 Execute 槽位。
8. 抢租约成功 → 提交线程池 → 任务结束后按结果更新 checkpoint 或冷却 worker。

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
  max_running_steps: 1          # 项目内并发的 Execute，1 = 串行；仅在 Step 互不抢资源时调大
  healthcheck_timeout: 20
  worker_healthcheck: startup_only   # startup_and_task | startup_only | disabled
  prompt_group: default
  max_questions: 1              # side questions answered in parallel with Decide/Execute
  max_summaries: 1              # background summaries folded in after a Decide
  context_budget:
    enabled: true
    context_window: 200000      # a worker's CONTEXT_WINDOW env overrides it
    max_output_tokens: 16000
    safety_ratio: 0.05
    safety_min_tokens: 2000
    safety_max_tokens: 18000
    recent_items: 20
    summary_max_calls: 1        # 0 disables the rolling summary
    summary_timeout: 90
    min_snapshot_tokens: 2000
tasks:
  decide:
    timeout: 300
    max_steps: 3
  execute:
    timeout: 600
    conclude_timeout: 90
  question:
    timeout: 120
container:
  image: cairn-worker-container:latest
  network_mode: host
  completed_action: stop        # stop | remove
  cap_add: []
local:
  workspace_root: null
  completed_action: keep        # keep | remove
tool_guard:                     # pi 扩展在 worker 内拦截 bash，dispatcher 只读配置
  enabled: false
  api_url: "http://127.0.0.1:8001"
  fail_action: allow            # allow | deny：服务端不可达时的行为
web:                            # cairn-nginx 前端的 Basic Auth，dispatcher 不使用
  auth:
    realm: Cairn
    users: []
workers:
  - name: pi-a
    type: pi                    # pi | mock
    task_types: [decide, execute]
    max_running: 2
    priority: 0
    env: {}
```

约束：

- worker 名字唯一；
- `max_project_workers` 不得超过 `max_workers`；
- `task_types` 只能取 `decide` / `execute`（Question 由具备 `decide` 能力的 worker 顺带作答）；
- `max_running_steps >= 1`，默认 1 保持项目内串行；
- 每个 worker 的 env 会和 `common_env` 合并，worker 自身优先；
- 顶层、`runtime`、`workers[]` 及 `web` 配置均为严格模式，出现未知键会直接启动失败（拼错的键不会被静默忽略）。

## 命令审核（Guard + Reviewer）

平台的立场是「默认放行，拦住不该发生的破坏」：渗透任务本来就要跑工具、装环境、上传 payload，
所以守卫不是白名单闸机，而是**审核员**。每次 `bash` 调用都在 pi 扩展里阻塞，判定分三层：

| 层 | 判定者 | 结果 |
|---|---|---|
| 1 | 确定性规则（`tool_rules`，priority 降序，首个命中） | `allow` / `deny` / `ask` |
| 2 | 未命中任何规则 → LLM 审核员 | `allow` / `deny` / `ask` |
| 3 | `ask` | 进人工队列，超时按 `timeout_action`（默认 deny） |

审核员不是黑名单，它用**两根轴**判断当前这一次调用的直接效果：

- **可逆性**：只读、探测、注入探测恒可逆；覆盖/删除/清空/改配置/停服务不可逆。
- **作用对象**：本次测试的产物，还是目标的生产资产（真实业务数据、真实账号、服务配置、在服务的进程）。

合成规则：`不可逆 且 作用于生产资产 = deny`；判不明且判错代价是真实破坏 = `ask`；其余 `allow`。
条款编号固定，写进审计的「命中规则」字段：

| 编号 | 含义 |
|---|---|
| D1–D2 | 改账号可用性/口令；改账号与权限体系、建高权账号 |
| D3 | 改系统与服务配置、防火墙、启动项 |
| D4 | 破坏真实业务数据（`DELETE/PUT/PATCH` 删真实记录、`DROP/TRUNCATE`、无条件 `UPDATE/DELETE`、清库、删生产文件） |
| D5–D6 | 停/禁在服务的进程；明示 flood 类 DoS |
| A1–A6 | 利用 payload、本次测试产物、口令测试、侦察、只读（含读 `/etc/shadow`、`--dump`）、落脚侦察 |
| ASK | 写/删/改但归属或影响面判不明 |

输入边界是硬契约（与 ARTEX 的 judge 一致）：

- 只给**当前调用**的完整参数、工作目录、命令引用脚本的源码快照、项目 `scope` 与 `constraints`；
- **不给**任务描述、计划、历史调用或历史裁决——「计划」会被模型当成额外授权，「历史」会把一次小拒绝放大成猜谜；
- 所有字段视为不可信数据，命令或脚本里的提示注入按数据处理；
- 脚本快照由 worker 内的扩展采集（`bash ./x.sh` 这类命令的唯一信息来源），单文件 64KB、最多 5 个，超限标记 `truncated`；读不到标 `complete=false`。

失败与超时策略：

| 情形 | 行为 |
|---|---|
| 审核员超时/传输失败/输出不合契约 | 重试 `CAIRN_REVIEWER_RETRY_COUNT` 次，仍失败按 `fail_action`（默认 `ask` 转人工） |
| 合法裁决 | `deny`/`ask` 即终局，不重试 |
| 证据不完整（`truncated`/读失败） | `incomplete_script_action`：`model`（交给审核员）或 `ask` |
| 人工审批超时 | `timeout_action`（默认 deny） |
| 审核员未配置 | 回到默认 `allow`，与无守卫部署一致 |

缓存：`allow`/`deny` 写入 `tool_review_cache`，键为「规范化命令 + 项目 + scope/constraints 摘要」。
改动 `scope` 或 `constraints` 会换 key，旧裁决自然失效；`ask` 永不缓存（人工裁决不能被回放）。

拒绝话术统一以「【Cairn 平台管控·非目标防御】」开头：渗透 agent 把平台拦截误读成目标 WAF 时会
换手法反复绕，越绕越糟。

审核员配置走 **server 环境变量**（不写进 `dispatch.yaml`——那份文件被 nginx 容器挂载）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CAIRN_REVIEWER_ENABLED` | `0` | 总开关 |
| `CAIRN_REVIEWER_MODEL` / `_BASE_URL` / `_API_KEY` | 空 | OpenAI 兼容端点；三者缺一即视为未配置 |
| `CAIRN_REVIEWER_API` | `openai-completions` | 目前只支持该协议 |
| `CAIRN_REVIEWER_TIMEOUT_SECONDS` / `_RETRY_COUNT` | `15` / `2` | 单次超时与重试次数 |
| `CAIRN_REVIEWER_FAIL_ACTION` | `ask` | 审核失败时的动作 |
| `CAIRN_REVIEWER_ASK_TIMEOUT_SECONDS` / `_ASK_TIMEOUT_ACTION` | `300` / `deny` | 人工队列超时 |
| `CAIRN_REVIEWER_INCOMPLETE_SCRIPT_ACTION` | `model` | 证据不完整时 `model` 或 `ask` |
| `CAIRN_REVIEWER_CACHE_TTL_SECONDS` | `86400` | 裁决缓存有效期 |
| `CAIRN_REVIEWER_POLICY_FILE` | 空 | 覆盖内置策略文本（运维可调，不必重建镜像） |

已知绕过路径（保留 `sudo` 的代价，必须靠部署侧兜住）：

1. worker 内 root 可改 `/etc/hosts` 或 `iptables` 让审核端点不可达 → 走 `fail_action`。把
   `fail_action` 设为 `deny` 可堵住，代价是审核服务抖动会中断扫描。
2. worker 可调 `POST /projects/{id}/tool-rules` 写「全部放行」的规则 → 规则写入端点必须只对 UI
   通道开放，worker 通道只给 check 与审批查询（服务令牌，见架构债清单）。
3. local 模式曾经用 `--no-extensions` 启动且不加载扩展，审核为零 → 现在 local 分支同样用
   `-e "$guard"` 显式加载，且有测试守住。
4. `tmux`/后台常驻会话里后续命令不再逐条经过 `bash` 工具 → 至少要求启动命令本身过审，并在
   report 中标注存在未逐条审核的子进程。

## 任务模型

### Decide

#### 职责

只读和修改图：评估 Step、添加 / 废弃 / 调整优先级、提出和关闭子 Goal、宣告根 Goal 达成。没有任何作用于世界的工具。

权限边界由契约保证：Decide 的输出即使包含世界操作也会被忽略，Dispatcher 只应用图变更。若需要进程级隔离，可在驱动层进一步限制 CLI 可用工具（pi 提供 `--tools` 白名单），但这不属于协议要求。

#### 触发条件

`_decide_trigger` 命中任一即触发：

| 触发 | 说明 |
|------|------|
| `initial` | 项目从未 Decide 过（没有 checkpoint） |
| `facts:N->M` | 事实增加 |
| `hints:N->M` | 提示增加 |
| `findings:N->M` | Finding 增加 |
| `open_steps:N->0` | 所有 Step 结束，需要重新规划 |
| `stuck:s062,...` | 剩下的 pending Step 都已烧完 Execute 次数（`MAX_STEP_EXECUTE_ATTEMPTS`），必须换路线或 abandon |
| `idle:1..3` | 图已全终结但项目仍 active，给 Decide 有界预算宣布完成 |

`stuck:` / `idle:` 是"图本身没有要求"的 Decide：每个 checkpoint 只有
`MAX_UNPROMPTED_DECIDE_RUNS` 次预算，且只在真正派发时扣减；图发生变化后预算重置。
预算花完后调度器不再重跑这些 Step，只按 WARNING 记录卡住的 Step——宁可停在死局，
也不要无限消耗 token。

checkpoint 在 Decide 成功后**重新读取图**再写入，这样"Decide 只废弃了 Step"也会在 Step 清空时再次触发。

#### 输入

- `{graph_yaml}`：图快照的容器内路径（大图不直接塞进提示词）。
- `{fact_ids}`：可引用的事实 id 列表。
- `{open_steps}`：pending / running 的 Step 摘要。失败过的 Step 额外带
  `failures: {count, last_error, last_at}`：这是调度器内存里该 Step 连续失败的 Execute
  轮数与最近一次原因（超时、退出码、解析失败等）。这些失败不会进入图，模型否则只会看到
  一个"还在 pending"的 Step 并原样重发同一条计划；`count >= 2` 时提示词要求换路线、拆小步
  或带说明地 abandon。连续失败达到 `MAX_STEP_EXECUTE_ATTEMPTS`（3）后调度器**不再重派**
  该 Step，并在它是图上最后一个可跑的 Step 时用 `stuck:` 触发 Decide。Step 成功或废弃、
  项目离开 active 时计数清零（废弃的 Step 在下一轮派发前就被丢弃）。
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
- `complete` 只有在没有任何 open Step、没有未关闭子 Goal 时才生效。同一 payload 里用
  `steps.update` 废弃掉最后的 **pending** Step、用 `goals.close` 关掉最后的子 Goal 是正常收尾
  （先落图操作，最后原子完成）。
- `running` 的 Step 不能在这个 payload 里被关闭：它归执行它的 worker，服务端会拒绝废弃，
  `complete` 也随之不生效——等它 `conclude` 出事实后下一轮 Decide 再完成。
- 携带 `goals.add` / `steps.add` / 只改 `priority`、`note` 的 `steps.update` 时，图操作保留、
  `complete` 丢弃；既没有任何图操作又有 open Step 的裸 `complete` → 失败。
- 没有任何动作但仍有 open Step → **noop**：合法的“等待”（典型场景是所有 Step 都在跑），记 success、
  不动图，只保存本轮的 recall / doc 请求。
- 没有任何动作且当前没有 open Step → 失败（必须推进：加 Step、关 Goal 或 complete）。
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

存在 `pending`、`worker` 为空、且本地没有在跑的 Step。按 `(priority, created_at, id)` 从大到小填充
`runtime.max_running_steps` 个槽位（已有 `N` 个 Execute 在跑时只再派发 `max_running_steps - N` 个）。

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
| 连续失败 | worker 进入 rejected 冷却（项目 + 任务类型维度，`REJECTED_RETRY_AFTER_SECONDS` = 5s） |
| Decide 失败（解析失败 / 完成被拒 / 崩溃） | checkpoint 不推进，同一 trigger 会立刻再次触发；按项目冷却 `DECIDE_RETRY_AFTER_SECONDS`（10s）后再派发 |
| 同一 trigger 连续失败达 `MAX_CONSECUTIVE_DECIDE_FAILURES`（5） | 不再派发该 trigger，直到图变化（trigger 字符串随之改变）；否则冷却只是把"无限重试"摊慢成每 10s 一次 |
| Decide 成功但图没变（noop） | 记 success，不写图；下一轮由 open Step / facts 变化继续驱动 |
| 调度循环单轮抛非 HTTP 异常 | `LOG.exception` 后 sleep 一个 interval 继续，不让整个 dispatcher 退出 |

### Question（旁路提问）

人类在任务进行中提的问题走独立通道：它不抢 Step、不写图，也不占用 Decide / Execute 的并发额度。

- dispatcher 每轮先 `GET /questions/pending` 取最早的待答问题，`runtime.max_questions` 限制同时作答数（默认 1）。
- 用 `decide` 能力的 worker 作答；抢占成功（`claim`）后才提交到独立的提问线程池。
- 提示词模板 `question.md` 可以不含 `{graph_yaml}`（mock 组就没有），此时不写快照、不启动容器。
- 答案只回写到 `questions` 表；问题被取消后提交的答案由服务端丢弃（`409`）。
- 提问失败（超时、退出码非零、空输出）同样写 `failed` 与 `error`，不会中断调度循环。

### 上下文预算

`{graph_yaml}` 不再无条件塞进提示词。`runtime.context_budget` 按 worker 的窗口算预算：

```
input_budget    = context_window - max_output_tokens - safety_margin
safety_margin   = clamp(window * safety_ratio, safety_min_tokens, safety_max_tokens)
snapshot_tokens = max(min_snapshot_tokens, input_budget - overhead_tokens)
```

`overhead_tokens` 是模板本身加上其他 JSON 块（事实 id、开放 Step 等）的开销，先从预算里扣掉。

快照装不下时按层退化，三条历史通道各自有摘要兜底：

| 通道 | 保留原文 | 折叠后 | 摘要上限（level 1-2 / 3） |
| --- | --- | --- | --- |
| Facts | 最近 `recent_items` 条 | `older_fact_ids` + `confirmed_facts_summary` | 2400 / 1200 字符 |
| 已关闭 Step | 最近 `recent_items` 条 | `older_step_ids` + `closed_steps_summary` | 1600 / 400 字符 |
| Findings | 最近 `recent_items` 条 | `older_finding_ids` + `findings_summary` | 1200 / 300 字符 |

没有摘要时三条通道都会生成确定性的逐条兜底文本，所以 `older_*_ids` 始终可引用。三层都装不下才硬截断并在 YAML 尾部留标记。

**摘要不阻塞 Decide。** Decide 只把本轮装不下的历史（`SummaryRequest`）交给调度器的摘要线程池（`runtime.max_summaries`，默认 1），本轮继续用上一版摘要；摘要任务完成后写入服务端，下一轮 Decide / Execute / 提问自动用上。服务端按通道做单调合并（覆盖数不小于已存值才替换），因此晚到的旧摘要不会覆盖新摘要。同一项目在途的摘要请求会被去重。

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
| 项目内 Execute 并发 | `runtime.max_running_steps` |
| 单 worker 并发 | `workers[].max_running` |
| 项目内 Decide 串行 | Server 的 decide 租约 |
| 单 Step 独占 | Server 的 step 租约 |
| 旁路提问并发 | `runtime.max_questions`（不计入上面的额度） |

## Worker

### 字段

| 字段 | 说明 |
|------|------|
| `name` | 唯一名称，出现在租约与日志里 |
| `type` | `pi` / `mock` |
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
extract_usage(stdout, stderr) -> Usage | None
supports_conclude() -> bool
local_binary() -> str | None          # 本地模式启动时要检查的宿主机 CLI
check_health(worker, *, timeout) -> HealthResult
describe_health(worker) -> str        # 健康检查结果里打印的端点
```

`build_execute` 返回 argv 与 session；session 用于把 conclude 兜底续接到同一次对话。

### 健康检查

- `startup_and_task`：启动时检查一次，每个任务前再检查；任务返回 `unhealthy` 时该 worker 冷却 `UNHEALTHY_RETRY_AFTER_SECONDS`（5s）。
- `startup_only`：只在启动时检查。
- `disabled`：完全跳过自动检查（默认值是 `startup_only`；`dispatch.local.example.yaml` 显式设为 `disabled`）。
- 本地模式启动时改为检查每个 worker 的 CLI 是否在 PATH 上（`local_binary()`），一个都没装则启动失败，部分缺失只告警。

### CLI 接入约定

Agent CLI 只需满足三点：

1. 能从 argv 接收完整提示词；
2. 最终把结构化 JSON 打到 stdout（允许夹杂文本 / markdown 代码块）；
3. 最好以流式 JSON 输出过程事件（`pi --mode json`），否则 Session 里只留得下最终回答。

Dispatcher 负责从输出中提取最终回答（`extract_response_text`），并把事件流展开成对话记录（`session.build_messages`）。降级路径始终保留：拿不到结构化事件时，原始 stdout / stderr 会作为一条 `tool` 消息写入 Session。

## 提示词资源

```
cairn/src/cairn/dispatcher/prompts/
  default/decide.md
  default/execute.md
  default/execute_conclude.md
  default/question.md
  mock/decide.md
  mock/execute.md
  mock/execute_conclude.md
  mock/question.md
  mock/summary.md
```

- `prompt_group` 选择目录。
- 加载时校验必需占位符，缺一即启动失败（`default` 组还额外要求 `{constraints}` / `{steers}` / `{scope}` / `{opsec}`）。
- default 组与安全任务解耦：只描述 Fact / Goal / Step 与输出契约。
- `default/` 下没有 `summary.md`：背景摘要任务会在读取失败时回退到内置模板；`mock/` 组提供了 `summary.md` 方便端到端验证。

## Mock 驱动

用于无 LLM 的端到端验证。每个阶段通过环境变量配置行为：

| 环境变量 | 阶段 | 可选结果 |
|----------|------|----------|
| `MOCK_HEALTHCHECK` | 健康检查 | `ok` / `fail` |
| `MOCK_DECIDE` | Decide | `complete` / `steps` / `noop` / `rejected` / `invalid_json` / `invalid_payload` / `command_fail` |
| `MOCK_EXECUTE` | Execute | `facts` / `rejected` / `invalid_json` / `invalid_payload` / `command_fail` |
| `MOCK_EXECUTE_CONCLUDE` | 兜底 | 同上 |
| `MOCK_QUESTION` | 旁路提问 | `answer` / `invalid_json` / `command_fail` |
| `MOCK_SUMMARY` | 背景摘要 | `answer` / `invalid_json` / `command_fail` |

支持按提示词内容设置规则，例如：

```json
{"delay": [0, 0], "outcomes": {"steps": 1},
 "rules": [{"fact_ids_gte": 2, "force": "complete"}]}
```

可用条件：`fact_ids_gte` / `fact_ids_lte` / `open_steps_empty`（未知条件键会被静默忽略）。

## 本地模式

`runtime.execution: local` 时不创建容器，直接在宿主机启动 CLI 进程：

- 每个项目一个工作目录 `<workspace_root>/<project_id>/`；
- 图快照写到宿主机临时目录；
- 复用宿主机已登录的 `pi`；
- 没有沙箱，只应在有授权的环境使用。

## 已知限制

- **硬截断仍可能截断结构**：预算极端时 `_hard_cut` 会在字符边界截断，YAML 可能不完整（尾部有截断标记，`context.note` 也会说明历史被压缩）。真实的错误信息在 `older_*_ids` 与摘要里，需要细节时应重新导出图。
- **SSE 仍是轮询式**：`sessions/stream` 与 `questions/stream` 已改成 `async def` + `asyncio.sleep`，不再占用线程池，但每个连接仍按秒查询 SQLite；连接数极高时可以改成事件推送（写库时通知）。
