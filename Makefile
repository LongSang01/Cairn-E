# Cairn image build: the app and the worker are independent images with no
# shared base, so they can build in parallel and each only rebuilds on its own
# changes.
.PHONY: app worker all up check

app:
	docker build -t cairn-app .

worker:
	docker build -t cairn-worker-container:latest ./container

all: app worker

up: all
	docker compose up -d

# 本地质量门（不依赖 CI）：lint + 格式 + 类型 + 测试。
# 运行环境需已安装 ruff/mypy/pytest（uv sync 或 pip install）。
check:
	cd cairn && python -m ruff check src tests
	cd cairn && python -m ruff format --check src tests
	cd cairn && python -m mypy src
	cd cairn && python -m pytest -q
