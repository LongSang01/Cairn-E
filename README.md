## Cairn-E

基于 **[Cairn](https://github.com/astral-sh/cairn)** 二开的版本

使用多个AI调整实现，姑且能用，生产一些电子垃圾

基于原作者 **[l3yx](https://l3yx.github.io/)** 对于 `Cairn_Y`的设计思路让模型进行实现，同时吸取 **`Autumn-27/ARTEX`(已删库)** 的设计进行优化

仅测试在`docker`下的部署

### Docker Compose (已验证)

系统镜像与 worker 镜像共用同一个基础镜像 `cairn-base`，必须按顺序构建：

```bash
make base                      # cairn-base:latest
make worker                    # cairn-worker-container:latest（dispatcher 使用）
make app                       # cairn-app（server + dispatcher 使用）
```

或者一次做完：

```bash
make all
docker compose up --build
```

只重建系统镜像时 `make app` 即可；`docker compose up --build` 不会自动构建 base。
