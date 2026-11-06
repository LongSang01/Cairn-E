# Cairn image build: the app and the worker are independent images with no
# shared base, so they can build in parallel and each only rebuilds on its own
# changes.
.PHONY: app worker all up

app:
	docker build -t cairn-app .

worker:
	docker build -t cairn-worker-container:latest ./container

all: app worker

up: all
	docker compose up -d
