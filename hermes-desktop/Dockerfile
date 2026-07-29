# Hermes Desktop VM
# Persistent desktop "computer body" for Hermes Agent.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:1 \
    VNC_PORT=5901 \
    NOVNC_PORT=6080 \
    VNC_RESOLUTION=1920x1080 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg locales tzdata \
    xfce4 xfce4-terminal dbus-x11 \
    tigervnc-standalone-server tigervnc-common tigervnc-tools novnc websockify \
    xdotool scrot imagemagick xclip xsel \
    net-tools iputils-ping dnsutils \
    python3 python3-pip python3-venv nodejs npm \
    git unzip jq htop nano fonts-liberation fonts-dejavu-core \
    && install -d -m 0755 /etc/apt/keyrings \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && locale-gen en_US.UTF-8 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash hermes \
    && mkdir -p /home/hermes/.vnc /home/hermes/Desktop /home/hermes/workspace /home/hermes/bin /home/hermes/.config/google-chrome \
    && chown -R hermes:hermes /home/hermes

USER hermes
WORKDIR /home/hermes
ENV HOME=/home/hermes \
    USER=hermes \
    PATH=/home/hermes/.local/bin:${PATH}

# Install the same upstream cua-driver used by Hermes's built-in computer_use
# tool. The host-side wrapper only transports its stdio MCP protocol through
# Docker; policy and approval remain in Hermes.
RUN curl -fsSL https://cua.ai/driver/install.sh -o /tmp/install-cua-driver.sh \
    && /bin/bash /tmp/install-cua-driver.sh --bin-dir /home/hermes/.local/bin --no-modify-path \
    && rm /tmp/install-cua-driver.sh

RUN printf '#!/bin/bash\nunset SESSION_MANAGER\nunset DBUS_SESSION_BUS_ADDRESS\nexport DISPLAY=:1\nexec startxfce4\n' > /home/hermes/.vnc/xstartup \
    && chmod +x /home/hermes/.vnc/xstartup \
    && printf '#!/bin/bash\nexport HOME=/home/hermes\nexport DISPLAY=:1\nexport XAUTHORITY=/home/hermes/.Xauthority\nURL="${1:-about:blank}"\nmkdir -p /home/hermes/.config/google-chrome\nif ! pgrep -u hermes -f "[g]oogle-chrome|/opt/[g]oogle/chrome/chrome" >/dev/null 2>&1; then\n  rm -f /home/hermes/.config/google-chrome/SingletonLock /home/hermes/.config/google-chrome/SingletonCookie /home/hermes/.config/google-chrome/SingletonSocket\nfi\nsetsid -f /usr/bin/google-chrome --no-sandbox --disable-dev-shm-usage --no-first-run --no-default-browser-check --disable-gpu --disable-session-crashed-bubble --no-restore-session-state --hide-crash-restore-bubble --window-position=0,0 --window-size=1400,900 "$URL" >/tmp/hermes-open-chrome.log 2>&1 < /dev/null\n' > /home/hermes/bin/open-chrome \
    && chmod +x /home/hermes/bin/open-chrome \
    && printf '[Desktop Entry]\nType=Application\nName=Google Chrome\nExec=/home/hermes/bin/open-chrome\nIcon=google-chrome\nTerminal=false\nCategories=Network;WebBrowser;\n' > /home/hermes/Desktop/chromium.desktop \
    && printf '[Desktop Entry]\nType=Application\nName=Terminal\nExec=xfce4-terminal\nIcon=utilities-terminal\nTerminal=false\nCategories=System;TerminalEmulator;\n' > /home/hermes/Desktop/terminal.desktop \
    && chmod +x /home/hermes/Desktop/chromium.desktop /home/hermes/Desktop/terminal.desktop

USER root

COPY start-desktop.sh /usr/local/bin/start-desktop.sh
COPY cua-driver-entrypoint /usr/local/bin/cua-driver-desktop
RUN chmod +x /usr/local/bin/start-desktop.sh /usr/local/bin/cua-driver-desktop

EXPOSE 5901 6080

CMD ["/usr/local/bin/start-desktop.sh"]
