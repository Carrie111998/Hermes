ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY gateway/ /opt/hermes/gateway/
COPY hermes_cli/ /opt/hermes/hermes_cli/
COPY tools/ /opt/hermes/tools/
COPY tui_gateway/ /opt/hermes/tui_gateway/

LABEL org.opencontainers.image.title="Hermes autonomous-room UAT"
