## Cairn-E

基于 **[Cairn](https://github.com/astral-sh/cairn)** 二开的版本

使用多个AI调整实现，姑且能用，生产一些电子垃圾

基于原作者 **[l3yx](https://l3yx.github.io/)** 对于 `Cairn_Y`的设计思路让模型进行实现，同时吸取 **`Autumn-27/ARTEX`(已删库)** 的设计进行优化

仅测试在`docker`下的部署

### Docker Compose (已验证)

系统镜像（`cairn-app`）与 worker 镜像（`cairn-worker-container`）相互独立、不共享基础镜像，可并行构建：

```bash
make worker                    # cairn-worker-container:latest（dispatcher 使用）
make app                       # cairn-app（server + dispatcher 使用）
```

或者一次做完：

```bash
make all
docker compose up --build
```

只重建系统镜像时 `make app` 即可；只重建 worker 时 `make worker` 即可。

### 命令审核（可选）

worker 里每条 `bash` 都会先问服务端。确定性规则没命中时，可以交给一个 LLM 审核员判断「这一次
调用是否会对目标生产资产造成不可逆破坏」——它只看当前调用、工作目录、脚本快照、项目 `scope`
与 `constraints`，不看计划与历史。未配置审核员时行为不变（默认放行）。

```bash
# .env（docker compose 自动读取）。不要写进 dispatch.yaml：那份文件由 dispatcher
# 与 server 共读，审核员密钥只应留在服务端环境变量里。
CAIRN_REVIEWER_ENABLED=1
CAIRN_REVIEWER_MODEL=<judge model>
CAIRN_REVIEWER_BASE_URL=https://<endpoint>/v1
CAIRN_REVIEWER_API_KEY=<key>
# 失败策略：ask 转人工（默认），deny 宁停不停
CAIRN_REVIEWER_FAIL_ACTION=ask

# 通道隔离（可选；留空则不强制）。建议用随机串：
# worker 侧扩展用它访问 /internal/*（dispatcher 注入，目标拿到 shell 也只能调 check）
CAIRN_WORKER_TOKEN=<random>
# 浏览器侧的认证请用下面的 web.auth；CAIRN_UI_TOKEN 只给脚本客户端，
# 且不要同时设置在 compose 里，否则浏览器改不了规则/批不了审批。
```

浏览器侧认证在服务端完成，写在 `dispatch.yaml`：

```yaml
web:
  auth:
    realm: Cairn
    users:
      - name: admin
        password: "<password>"
```

配置后所有页面、API 与 SSE 都要求 Basic Auth；`/healthz` 与 worker 的 `/internal/*` 除外（后者
仍由 `CAIRN_WORKER_TOKEN` 保护）。未配置 `web.auth` 时服务端拒绝绑定非回环地址，除非显式加
`--allow-insecure`。

判定分级、D/A 条款与已知绕过路径见 `docs/specs/dispatcher-design.md` 的「命令审核」一节。

### 本地质量门

不依赖 CI，一条命令跑完 lint、格式、类型、Python 测试与前端测试：

```bash
make check
```

它会依次执行（需已安装 `ruff` / `mypy` / `pytest`，并执行过 `npm install`）：

1. `cd cairn && python -m ruff check src tests`
2. `cd cairn && python -m ruff format --check src tests`
3. `cd cairn && python -m mypy src`
4. `cd cairn && python -m pytest -q`
5. `npm run test` 与 `npm run format:check`（vitest / prettier）

只跑 Python 测试时，注意 pytest 需要从 `cairn/` 目录执行（`pythonpath = ["src"]`）：

```bash
cd cairn && python -m pytest
cd cairn && python -m pytest --cov=src/cairn --cov-report=term-missing   # 覆盖率
```
