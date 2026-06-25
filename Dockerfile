ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21
FROM ${BUILD_FROM}

# Install system dependencies
RUN apk add --no-cache \
    build-base \
    curl \
    nodejs-current \
    npm \
    python3-dev \
    ripgrep \
    ffmpeg \
    gcc \
    libffi-dev \
    openssl-dev \
    pkgconfig \
    procps \
    git \
    openssh-client \
    docker-cli \
    tini \
    coreutils \
    chromium \
    nginx \
    tmux \
    ttyd \
    openssl \
    apache2-utils \
    bash-completion \
    jq

# Nginx setup
RUN mkdir -p /run/nginx /var/log/nginx /var/www && \
    chown -R hermes:hermes /run/nginx /var/log/nginx /var/www /etc/nginx

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_TUI_DIR=/opt/hermes/ui-tui
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser

WORKDIR /opt/hermes
# Non-root user for runtime
RUN adduser -u 10000 -D -h /opt/hermes hermes


# Layer-cached dependency install
COPY package.json package-lock.json ./
COPY web/package.json web/package-lock.json web/
COPY ui-tui/package.json ui-tui/package-lock.json ui-tui/
COPY ui-tui/packages/hermes-ink/ ui-tui/packages/hermes-ink/

ENV npm_config_install_links=false

RUN npm install --prefer-offline --no-audit && \
    (cd web && npm install --prefer-offline --no-audit) && \
    (cd ui-tui && npm install --prefer-offline --no-audit) && \
    npm cache clean --force

# Python dependencies
COPY pyproject.toml uv.lock ./
RUN touch ./README.md
RUN uv sync --frozen --no-install-project --extra all --extra messaging

# Source code
COPY --chown=root:root . .

# Build assets
RUN cd web && npm run build && \
    cd ../ui-tui && npm run build

# Permissions for runtime
RUN chmod -R a+rX /opt/hermes && \
    chown -R hermes:hermes /opt/hermes/.venv /opt/hermes/ui-tui /opt/hermes/node_modules

# Link hermes-agent
RUN uv pip install --no-cache-dir --no-deps -e "."

# Runtime PATH to include .venv
ENV PATH="/opt/hermes/.venv/bin:${PATH}"

# Copy add-on entrypoint
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ARG BUILD_ARCH
LABEL \
    io.hass.name="Hermes Agent" \
    io.hass.description="The self-improving AI agent by Nous Research" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="0.17.0"

ENTRYPOINT [ "/sbin/tini", "--", "/run.sh" ]
