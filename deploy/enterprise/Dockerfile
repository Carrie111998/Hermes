# Hermes Enterprise image — serves both the OCC controller and the
# per-namespace gateway pods from a single build.
#
#   Controller:  python -m enterprise.cli            (default ENTRYPOINT)
#   Gateway:     python -m hermes_cli.main gateway run   (override args)
#
# The repo-root Dockerfile builds the full published container (node, s6,
# playwright, pinned sqlite). This image is deliberately much smaller: the
# enterprise control plane and headless gateways need only the Python
# package. We follow the root image's conventions where they apply:
# venv-based install, non-root runtime user, PYTHONUNBUFFERED /
# PYTHONDONTWRITEBYTECODE, writable state on a mounted volume.

# ---------- Build stage ----------
FROM python:3.12-slim AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get -o Acquire::Retries=3 update && \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        gcc g++ make libffi-dev git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Layer-cache the dependency resolve on the manifest alone (mirrors the
# root Dockerfile's manifest-first COPY). pyproject.toml references
# README.md, so bring it too.
COPY pyproject.toml README.md ./
RUN python -m venv /opt/hermes/.venv && \
    /opt/hermes/.venv/bin/pip install --upgrade pip

# Full source, then install the project into the venv.
COPY . .
RUN /opt/hermes/.venv/bin/pip install .

# ---------- Runtime stage ----------
FROM python:3.12-slim

ARG HERMES_GIT_SHA=
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="Hermes Enterprise" \
      org.opencontainers.image.description="Hermes Enterprise control plane (OCC controller) and per-namespace gateway runtime" \
      org.opencontainers.image.vendor="Nous Research" \
      org.opencontainers.image.source="https://github.com/NousResearch/hermes-agent" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${HERMES_GIT_SHA}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HERMES_HOME=/opt/data \
    PATH="/opt/hermes/.venv/bin:${PATH}"

RUN apt-get -o Acquire::Retries=3 update && \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        ca-certificates curl tini && \
    rm -rf /var/lib/apt/lists/*

# Same non-root convention as the root image (user "hermes", UID 10000,
# home on the data volume).
RUN useradd -u 10000 -m -d /opt/data hermes

COPY --from=build /opt/hermes/.venv /opt/hermes/.venv

# Bake the build revision the same way the root Dockerfile does so
# build_info / support triage can identify the commit.
RUN if [ -n "${HERMES_GIT_SHA}" ]; then \
        mkdir -p /opt/hermes && \
        printf '%s\n' "${HERMES_GIT_SHA}" > /opt/hermes/.hermes_build_sha; \
    fi

WORKDIR /opt/data
VOLUME [ "/opt/data" ]
USER hermes

# tini as PID 1 to reap subprocess zombies (the root image uses
# s6-overlay for full supervision; the enterprise pods run a single
# process each, so tini is sufficient).
ENTRYPOINT [ "/usr/bin/tini", "--", "python", "-m", "enterprise.cli" ]
# Controller default. Gateway pods override the whole command, e.g.:
#   command: ["/usr/bin/tini", "--", "python", "-m", "hermes_cli.main", "gateway", "run"]
CMD [ ]
