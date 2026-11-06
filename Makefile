# Cairn image build order: the shared base must exist before either derived
# image, and docker compose cannot express that dependency on its own.
BASE_IMAGE ?= cairn-base:latest

.PHONY: base app worker all up

base:
	docker build -f Dockerfile.base -t $(BASE_IMAGE) .

app: base
	docker build --build-arg CAIRN_BASE_IMAGE=$(BASE_IMAGE) -t cairn-app .

worker: base
	docker build --build-arg CAIRN_BASE_IMAGE=$(BASE_IMAGE) -t cairn-worker-container:latest ./container

all: app worker

up: all
	docker compose up -d
