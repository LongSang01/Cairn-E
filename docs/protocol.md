# Cairn 服务端协议(契约 1.0)

本文档冻结 **cairn-server 的对外 HTTP 契约**:REST 资源面、SSE 事件流语法与幂等写入语义。
契约版本常量定义于 [cairn/contracts/__init__.py](../cairn/src/cairn/contracts/__init__.py) 的
`CONTRACT_VERSION`,由 `/healthz` 与 `/metrics` 对外暴露,字段清单由
[tests/test_contract_freeze.py](../cairn/tests/test_contract_freeze.py) 守护。

## 1. 版本策略

- **additive-only**:1.x 内只允许新增可选字段/事件类型/路由;不删除、不改名、不改语义。
- 字段变更必须同步:冻结测试、本文档、`CONTRACT_VERSION` 的版本决策。
- 破坏性变更 ⇒ 升主版本号,并在过渡期内双写或提供迁移说明。
- 数据库演进走 `cairn/server/db.py` 的迁移台账(`schema_migrations`),只追加、不改写已发布的迁移。

## 2. 资源面(REST)

所有路由以 `/projects/{project_id}` 为前缀。会话(worker run)是本契约的核心实体。

### 2.1 创建会话

`POST /projects/{project_id}/sessions` → `201 SessionDetail`

请求体(`CreateSessionRequest`):

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `client_key` | string?, 可选 | **幂等键**。同一逻辑创建的重试必须携带相同值 |
| `worker` / `worker_type` / `phase` | string | worker 标识与阶段 |
| `step` | string? | 关联步骤 id |
| `summary` | string(≤300) | 会话摘要 |
| `status` | `running`\|`success`\|`failed` | **必填**,创建时的初始状态 |
| `returncode` / `timed_out` / `duration_ms` / `error` | 终态字段 | 非 running 创建即为终态创建 |
| `messages` | `SubmittedSessionMessage[]` | 首批消息(可空) |
| `*_tokens` | int | TokenUsage 五字段 |

**幂等语义**(`client_key` 相同的重复创建):

1. 返回**同一会话**(首次创建的 `201` + 同一 `id`),不产生第二个会话。
2. 消息合并:按 `msg_id` 去重后追加,未带 `msg_id` 的消息无法去重、总是追加。
3. 终态收敛:若现存会话仍为 `running` 且重试携带非 `running` 状态,则在此定格终态(幻影场景:迟到的 `running` 创建先于终态创建落库时,后到的终态创建仍可完成收尾)。
4. **不复活、不改写**:会话已终态后,任何 `running` 创建只合并消息,不改写状态与结果字段;冲突终态以先到者为准。

### 2.2 追加消息

`POST /projects/{project_id}/sessions/{session_id}/messages` → `200 SessionDetail`

请求体:`{"messages": SubmittedSessionMessage[]}`。

- **幂等键**:消息的 `msg_id`(客户端生成,重试保持不变)。
- 同一 `(session_id, msg_id)` 只落库一次,重复静默跳过(整体仍返回 `200`)。
- `seq` 只在真正插入时递增:被跳过的重试不占用序号,序号可能有空洞但**单调递增**(消费方用 `seq` 做增量游标,不得假设连续)。
- 会话已终态后追加 ⇒ `409`。

`SubmittedSessionMessage`:`role`、`content`、`raw`(可选原文)、`msg_id`(可选,但**没有它就没有幂等保证**)。

### 2.3 读取与增量

- `GET /projects/{project_id}/sessions` → `Session[]`(按创建时间倒序)。
- `GET /projects/{project_id}/sessions/{session_id}` → `SessionDetail`(含全量 `messages`)。
  - 支持 `ETag` / `If-None-Match` ⇒ 未变更返回 `304`。
  - 支持 `?since_seq=N` ⇒ 只返回 `seq > N` 的消息(增量拉取;会话级字段照常返回)。
- `PATCH /projects/{project_id}/sessions/{session_id}` → 终态/进度更新(幂等,可重复调用)。
- `POST /projects/{project_id}/sessions/{session_id}/heartbeat` → 续约 `updated_at`,不影响内容。

### 2.4 健康与观测

- `GET /healthz`:`{"status", "version", "database", "schema_version", "contract_version"}`;数据库不可达时 `503`。
- `GET /metrics`:Prometheus 文本格式,含 `cairn_schema_version`、`cairn_db_busy_total` 等。

## 3. SSE 事件流

`GET /projects/{project_id}/sessions/{session_id}/stream`
(`Content-Type: text/event-stream`)

### 3.1 事件类型

| event | data | 说明 |
| --- | --- | --- |
| `snapshot` | `{"session": Session, "messages": [...]}` | 仅在**非恢复**连接的首个事件发送;`id` = 已有消息的最大 `seq`,无消息时为 `-1` |
| `messages` | `{"session": Session, "messages": [...]}` | 增量批次;`id` = 批内最大 `seq` |
| `end` | `{"session": Session}` | 会话终态(或空闲超时),流关闭 |
| `gone` | `{"id": session_id}` | 会话/项目不存在,流关闭 |

- 保活:空闲时每个轮询周期(0.5s)输出一行 `: keep-alive` 注释(不是事件)。
- 空闲截断:dispatcher 失联超过 `STREAM_IDLE_LIMIT_SECONDS`(300s)时发送 `end` 并关闭。
- `id:` 行的值恒为该帧所含消息的最大 `seq`,即客户端的续传游标。

### 3.2 断点续传

客户端重连时带 `Last-Event-ID: <最后处理的 id>`(浏览器 EventSource 自动完成);
也支持查询参数 `?since_seq=N`(两者同传时以 `Last-Event-ID` 优先)。

恢复语义:

1. **不重发快照**,只回放 `seq > 游标` 的消息批次,随后恢复正常轮询(0.5s)。
2. 游标统一语义为 `seq > 游标`;空快照的 `id` 为 `-1`,使首次重连能回放含 `seq 0` 在内的全部历史。
3. 非法 `Last-Event-ID`(非整数)⇒ 回退全量快照,客户端仍可自愈。
4. 会话已终态时,恢复流回放完增量后发送 `end` 并关闭。

### 3.3 客户端使用要点

- 按 `id:` 游标去重/续传,不要依赖连接边界。
- 快照与增量按 `seq` 严格衔接(快照含到 `seq=N`,增量从 `N+1` 起),按 `seq` 合并天然幂等。
- 断线重连必须带 `Last-Event-ID`;不带则收到全量快照(正确但更重)。

## 4. 幂等设计与 dispatcher 约定

- **两层幂等键**:`client_key` 定位会话(创建),`msg_id` 定位消息(追加)。
- dispatcher 侧([cairn/dispatcher/session.py](../cairn/src/cairn/dispatcher/session.py)):
  - 每个 `SessionStream` 在启动时生成一个 `client_key`,**创建与终态补传共用**,使实时路径与兜底上传在服务端收敛到同一会话。
  - 消息在进入重试缓冲区之前盖上 `msg_id`(uuid4),重试不换号。
  - 上传重试(3 次,退避 2s/5s)全部可安全重放。
- 未带 `msg_id` 的消息与未带 `client_key` 的创建**不提供幂等保证**(重复投递即重复入库),手工调用方需自行携带。

## 5. 兼容性承诺(1.x)

- 事件类型只增不减;新事件旧客户端应忽略。
- JSON 字段只增不减;客户端必须容忍未知字段。
- `seq` 语义(单调递增游标)与 `id:` 约定不变。
- 状态枚举(`running`/`success`/`failed`)不变;新状态只在升主版本时引入。
