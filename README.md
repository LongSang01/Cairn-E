## Cairn-E

基于 **[Cairn](https://github.com/astral-sh/cairn)** 二开的版本

使用多个AI调整实现，姑且能用，生产一些电子垃圾

基于原作者 **[l3yx](https://l3yx.github.io/)** 对于 `Cairn_Y`的设计思路让模型进行实现，同时吸取 **`Autumn-27/ARTEX`(已删库)** 的设计进行优化

仅测试在`docker`下的部署

### Docker Compose (已验证)

```bash
make all
docker compose up --build
```

只重建系统镜像时 `make app` 即可；只重建 worker 时 `make worker` 即可。

### 命令审核（可选）

```bash
CAIRN_REVIEWER_ENABLED=1
CAIRN_REVIEWER_MODEL=<model_id>
CAIRN_REVIEWER_BASE_URL=https://<endpoint>/v1
CAIRN_REVIEWER_API_KEY=<key>
# 失败策略：ask 转人工（默认)
CAIRN_REVIEWER_FAIL_ACTION=ask

# 通道隔离（可选；留空则不强制）。建议用随机串：
CAIRN_WORKER_TOKEN=<random>
```

浏览器侧认证在服务端完成，写在 `dispatch.yaml`：

```yaml
web:
  auth:
    realm: Cairn
    users:
      - name: admin
        password: '<password>'
```

### 本地测试

不依赖 CI，一条命令跑完 lint、格式、类型、Python 测试与前端测试：

```bash
make check
```

它会依次执行（需已安装 `ruff` / `mypy` / `pytest`，并执行过 `npm install`）：

1. `cd cairn && python -m ruff check src tests`
2. `cd cairn && python -m ruff format --check src tests`
3. `cd cairn && python -m mypy src`
4. `cd cairn && python -m pytest -q`
5. `npm run test` 与 `npm run format:check`

只跑 Python 测试时，注意 pytest 需要从 `cairn/` 目录执行（`pythonpath = ["src"]`）：

```bash
cd cairn && python -m pytest
cd cairn && python -m pytest --cov=src/cairn --cov-report=term-missing   # 覆盖率
```
