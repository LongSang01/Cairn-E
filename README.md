## Cairn-E

基于 **[Cairn](https://github.com/astral-sh/cairn)** 二开的版本

使用多个AI调整实现，姑且能用，生产一些电子垃圾

基于原作者 **[l3yx](https://l3yx.github.io/)** 对于 `Cairn_Y`的设计思路让模型进行实现，同时吸取 **`Autumn-27/ARTEX`(已删库)** 的设计进行优化

仅测试在`docker`下的部署

### Docker Compose (已验证)

```bash
docker pull ghcr.io/astral-sh/uv:python3.13-trixie-slim
```

```bash
docker compose up --build
```
