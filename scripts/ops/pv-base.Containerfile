# pv-base — 피터보이스 클라우드 유저 컨테이너 이미지
#
# 빌드 (호스트에서, 빌드 컨텍스트에 호스트의 네이티브 claude 를 복사해 둔다):
#   mkdir -p ~/pv-base && cp scripts/ops/pv-base.Containerfile ~/pv-base/Containerfile
#   sudo cp /usr/local/lib/claude-native/claude ~/pv-base/claude-native
#   cd ~/pv-base && sudo nice podman build -t localhost/pv-base:<날짜> .
#
# 원칙
# - claude 는 **호스트와 같은 네이티브 빌드·같은 버전**. npm 빌드는 내장 ugrep 버그로 grep 한 번에 4GB OOM
#   (2026-08-25 jenn, docs/ops/cloud-claude-native-build.md). 버전이 어긋나면 옛 방식에서 쓰던 세션을 못 이을 수 있다
# - 옛 systemd 방식(호스트에서 직접 실행)에서 옮겨오는 유저가 쓰던 도구가 그대로 있어야 한다
#   (2026-09-17 호스트 대비 누락 점검: 빌드 도구·압축·wget/rsync, playwright chromium 실행 라이브러리)
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv python3-dev sudo \
      curl wget ca-certificates git ripgrep jq unzip zip xz-utils zstd bzip2 \
      file less rsync netcat-openbsd bc procps psmisc locales tzdata \
      build-essential \
      ffmpeg poppler-utils \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 libxdamage1 libatspi2.0-0 \
      libxrandr2 libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 libxfixes3 libdrm2 \
    && locale-gen en_US.UTF-8 ko_KR.UTF-8 \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/cloudflared.deb \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
    && dpkg -i /tmp/cloudflared.deb && rm -f /tmp/cloudflared.deb
COPY claude-native /usr/local/lib/claude-native/claude
RUN chmod 755 /usr/local/lib/claude-native/claude \
    && ln -sf /usr/local/lib/claude-native/claude /usr/local/bin/claude \
    && ln -sf /usr/local/lib/claude-native/claude /usr/bin/claude \
    # pv-service / pv-tunnel: 호스트 레포(scripts/cloud-bin)를 /opt/pv/bin 에 ro 마운트 (config container.bin_dir)
    && ln -sf /opt/pv/bin/pv-service /usr/local/bin/pv-service \
    && ln -sf /opt/pv/bin/pv-tunnel /usr/local/bin/pv-tunnel
ENV LANG=en_US.UTF-8 DISABLE_AUTOUPDATER=1
RUN useradd -m -u 10000 -s /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent
USER agent
WORKDIR /home/agent
CMD ["sleep", "infinity"]
