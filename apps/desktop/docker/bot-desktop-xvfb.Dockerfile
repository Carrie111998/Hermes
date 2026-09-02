FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
  && apt-get install --no-install-recommends -y \
    chromium \
    curl \
    openbox \
    python3 \
    websockify \
    x11-utils \
    x11vnc \
    x11-xserver-utils \
    xterm \
    xvfb \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# The Electron main process supplies the bounded Xvfb/openbox/browser/VNC
# launcher as the container command. Keeping the image stateless makes the
# per-profile workspace mount and explicit cleanup visible at the call site.
ENTRYPOINT ["/bin/sh"]
