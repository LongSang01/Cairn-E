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
