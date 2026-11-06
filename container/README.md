```
# 需要先构建共用基础镜像
docker build -f ../Dockerfile.base -t cairn-base:latest ..
docker build . -t cairn-worker-container
```
