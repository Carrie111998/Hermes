FROM mcr.microsoft.com/playwright@sha256:6446946a1d9fd62d9ae501312a2d76a43ee688542b21622056a372959b65d63d

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        docker.io \
        docker-buildx \
        docker-compose-v2 \
        jq \
        ripgrep \
    && npm install --global npm@10.9.2 \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY scripts/newtonsapple-pr-review-font-mocks.cjs /usr/local/share/review-font-mocks.cjs
COPY scripts/newtonsapple-pr-review-migrator.Dockerfile /usr/local/share/review-migrator.Dockerfile
COPY --chmod=0755 scripts/newtonsapple-pr-review-docker-wrapper /usr/local/bin/docker

WORKDIR /workspace
CMD ["sleep", "infinity"]
