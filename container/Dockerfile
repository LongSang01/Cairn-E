# Worker（agent 沙箱）镜像：agent 跑命令的渗透工具箱。
#
# 基于共用基础镜像（Debian 运行时 + 系统 Python + uv），这里只加 worker 专属
# 的内容：工具集、agent CLI、非 root 的 agent 用户。基础镜像来自：
#   docker build -f Dockerfile.base -t cairn-base:latest .
#   docker build -t cairn-worker-container:latest ./container
# `make worker` 会按顺序做完两步。
ARG CAIRN_BASE_IMAGE=cairn-base:latest
FROM --platform=amd64 ${CAIRN_BASE_IMAGE}

# ---------------------------------------------------------------------------
# 加速参数（生产可全部用 --build-arg 覆盖；直连环境传空值即可）
#   --build-arg GH_PROXY=
# ---------------------------------------------------------------------------
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG GH_PROXY=https://gh-proxy.com/

# 一层装完所有 apt 依赖：
#   通用工具 / 常用渗透工具（详见 AGENTS.md 的工具清单）
# base 已提供 ca-certificates / curl / python3 / python3-pip / python3-venv，不再重复装。
# 每类只留一个主力，避免同类工具重复占体积：
#   端口扫描 nmap + naabu（masscan 的活 naabu 能做，且速率不当容易打爆目标/本机网络）
#   目录爆破 ffuf（dirb 已淘汰，gobuster 的 dir/vhost/dns 也被 ffuf 覆盖）
#   DNS/子域 subfinder + dig（dnsrecon/fierce/dnsenum 与之高度重叠）
#   Web 指纹 httpx -td + nuclei（whatweb 冗余）
# 二层/欺骗/探测（dsniff/hping3/arping/onesixtyone/nbtscan）不再预装：
#   NetBIOS 用 nxc + enum4linux-ng，SNMP 用 nmap NSE，真需要时让 agent 现装。
# 刻意不收：metasploit（体积大、更新快）、seclists（1GB+）、tshark（依赖重，
# 抓包用 tcpdump）、playwright 浏览器（需要时由 agent 自己装）。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      wget git jq yq file less vim-tiny nano sudo \
      unzip zip xz-utils xxd ripgrep fd-find rsync procps psmisc \
      nodejs npm \
      tmux iputils-ping bind9-dnsutils net-tools iproute2 whois \
      netcat-openbsd socat sshpass rlwrap openssh-client \
      nmap sqlmap hydra john tcpdump \
      proxychains4 smbclient ldap-utils krb5-user python3-impacket python3-scapy \
      binutils strace; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Go 单文件工具：走 gh-proxy 拉取，比 apt/编译都省事。
RUN set -eux; cd /tmp; \
    wget -q "${GH_PROXY}https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip" -O nuclei.zip; \
    unzip -q nuclei.zip nuclei; mv nuclei /usr/local/bin/; \
    wget -q "${GH_PROXY}https://github.com/ffuf/ffuf/releases/download/v2.1.0/ffuf_2.1.0_linux_amd64.tar.gz" -O ffuf.tgz; \
    tar -xzf ffuf.tgz ffuf; mv ffuf /usr/local/bin/; \
    wget -q "${GH_PROXY}https://github.com/projectdiscovery/subfinder/releases/download/v2.6.7/subfinder_2.6.7_linux_amd64.zip" -O subfinder.zip; \
    unzip -q subfinder.zip subfinder; mv subfinder /usr/local/bin/; \
    wget -q "${GH_PROXY}https://github.com/projectdiscovery/httpx/releases/download/v1.6.10/httpx_1.6.10_linux_amd64.zip" -O httpx.zip; \
    unzip -q httpx.zip httpx; mv httpx /usr/local/bin/; \
    wget -q "${GH_PROXY}https://github.com/projectdiscovery/naabu/releases/download/v2.3.1/naabu_2.3.1_linux_amd64.zip" -O naabu.zip; \
    unzip -q naabu.zip naabu; mv naabu /usr/local/bin/; \
    wget -q "${GH_PROXY}https://github.com/jpillora/chisel/releases/download/v1.10.1/chisel_1.10.1_linux_amd64.gz" -O chisel.gz; \
    gzip -d chisel.gz; mv chisel /usr/local/bin/chisel; \
    wget -q "${GH_PROXY}https://github.com/ropnop/kerbrute/releases/download/v1.0.3/kerbrute_linux_amd64" -O /usr/local/bin/kerbrute; \
    chmod +x /usr/local/bin/*; \
    rm -rf /tmp/*

# Debian 仓库里没有的渗透工具。bloodyad / coercer 在 PyPI；
# netexec（PyPI 包已下架）与 enum4linux-ng（从未上架）从 GitHub 源码装。
# bloodyad / coercer 仍是老的 setup.py 打包，构建隔离环境里的 setuptools 会报
# "cannot import name 'setup'"，所以先升级构建工具再关掉隔离安装。
RUN set -eux; \
    pip3 install --break-system-packages --no-cache-dir --ignore-installed setuptools wheel; \
    pip3 install --break-system-packages --no-cache-dir \
      ldap3 pycryptodome jinja2 tqdm pyyaml; \
    # --ignore-installed：Debian 的 cryptography/impacket 没有 RECORD，pip 想升级
    # 时会报 uninstall-no-record-file；装到 /usr/local 覆盖即可。
    pip3 install --break-system-packages --no-cache-dir --no-build-isolation --ignore-installed \
      bloodyad coercer

RUN set -eux; cd /tmp; \
    # NetExec 的依赖里有 impacket@git+https://github.com/...，pip 会自己起 git clone；
    # 直连 GitHub 在国内构建机常失败，这里把 github.com 重写到 GH_PROXY 供子进程继承。
    if [ -n "${GH_PROXY}" ]; then \
      export GIT_CONFIG_COUNT=1 \
             GIT_CONFIG_KEY_0="url.${GH_PROXY}https://github.com/.insteadOf" \
             GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi; \
    git clone --depth 1 "${GH_PROXY}https://github.com/Pennyw0rth/NetExec.git" netexec; \
    # netifaces 没有 manylinux wheel，需要现编译；gcc/python3-dev 只在这一次 RUN 里
    # 装上用完即卸，最终镜像不留编译工具链。
    apt-get update; \
    apt-get install -y --no-install-recommends gcc python3-dev; \
    pip3 install --break-system-packages --no-cache-dir ./netexec; \
    apt-get purge -y --auto-remove gcc python3-dev; \
    git clone --depth 1 "${GH_PROXY}https://github.com/cddmp/enum4linux-ng.git" e4l; \
    install -d /opt/enum4linux-ng; \
    cp -a e4l/. /opt/enum4linux-ng/; \
    printf '%s\n' '#!/bin/sh' 'exec python3 /opt/enum4linux-ng/enum4linux-ng.py "$@"' \
      > /usr/local/bin/enum4linux-ng; \
    chmod +x /usr/local/bin/enum4linux-ng; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/netexec /tmp/e4l

# 三个 agent CLI 才是这个镜像作为 worker 的核心。
RUN npm config set registry "${NPM_REGISTRY}" \
 && npm install -g \
      @openai/codex@0.118.0 \
      @anthropic-ai/claude-code@2.1.98 \
      @mariozechner/pi-coding-agent@0.73.0 \
 && npm cache clean --force

RUN set -eux; \
    useradd -m -s /bin/bash agent; \
    echo "agent ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers; \
    mkdir -p /home/agent/.config/nuclei /home/agent/workspace; \
    printf '%s\n' \
      'disable-update-check: true' \
      'update-template-dir: /home/agent/.local/nuclei-templates' \
      > /home/agent/.config/nuclei/config.yaml; \
    printf '%s\n' \
      'PS1="${PS1%\n*} \[\033[0;36m\]\D{%y-%m-%d} - \t\[\033[;32m\]\n${PS1##*\n}"' \
      >> /home/agent/.bashrc; \
    chown -R agent:agent /home/agent

ENV TZ=Asia/Shanghai
ENV GH_PROXY=${GH_PROXY}
ENV PLAYWRIGHT_MCP_BROWSER=chromium
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV PYTHONUNBUFFERED=1

COPY --chown=agent:agent ./.agents /home/agent/workspace/.agents
COPY --chown=agent:agent ./.agents /home/agent/workspace/.claude
COPY --chown=agent:agent ./AGENTS.md /home/agent/workspace/AGENTS.md
COPY --chown=agent:agent ./AGENTS.md /home/agent/workspace/CLAUDE.md

USER agent
WORKDIR /home/agent/workspace
RUN git init -q
