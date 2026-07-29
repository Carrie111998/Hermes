#!/bin/bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:1}"
export USER=hermes
export HOME=/home/hermes

install -d -m 700 -o hermes -g hermes /home/hermes/.vnc
install -d -m 755 -o hermes -g hermes \
  /home/hermes/bin \
  /home/hermes/Desktop \
  /home/hermes/workspace \
  /home/hermes/.config/google-chrome

cat > /home/hermes/.vnc/xstartup << 'EOF'
#!/bin/bash
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export DISPLAY=:1
eval "$(dbus-launch --sh-syntax)"
printf 'export DBUS_SESSION_BUS_ADDRESS=%q\nexport DBUS_SESSION_BUS_PID=%q\n' \
  "$DBUS_SESSION_BUS_ADDRESS" "$DBUS_SESSION_BUS_PID" \
  > /home/hermes/.desktop-session-env
exec startxfce4
EOF
chmod +x /home/hermes/.vnc/xstartup
chown hermes:hermes /home/hermes/.vnc/xstartup

cat > /home/hermes/bin/open-chrome << 'EOF'
#!/bin/bash
set -e

URL="${1:-about:blank}"
export HOME=/home/hermes
export DISPLAY=:1
export XAUTHORITY=/home/hermes/.Xauthority

if ! pgrep -u hermes -f '[g]oogle-chrome|/opt/[g]oogle/chrome/chrome' >/dev/null 2>&1; then
  rm -f /home/hermes/.config/google-chrome/SingletonLock \
        /home/hermes/.config/google-chrome/SingletonCookie \
        /home/hermes/.config/google-chrome/SingletonSocket
fi

setsid -f /usr/bin/google-chrome \
  --no-sandbox \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --disable-session-crashed-bubble \
  --no-restore-session-state \
  --hide-crash-restore-bubble \
  --window-position=0,0 \
  --window-size=1400,900 \
  "$URL" >/tmp/hermes-open-chrome.log 2>&1 < /dev/null
EOF
chmod +x /home/hermes/bin/open-chrome
chown hermes:hermes /home/hermes/bin/open-chrome

cat > /home/hermes/Desktop/chromium.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Google Chrome
Exec=/home/hermes/bin/open-chrome
Icon=google-chrome
Terminal=false
Categories=Network;WebBrowser;
EOF

cat > /home/hermes/Desktop/terminal.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Terminal
Exec=xfce4-terminal
Icon=utilities-terminal
Terminal=false
Categories=System;TerminalEmulator;
EOF
chmod +x /home/hermes/Desktop/chromium.desktop /home/hermes/Desktop/terminal.desktop
chown hermes:hermes /home/hermes/Desktop/chromium.desktop /home/hermes/Desktop/terminal.desktop

if [ -n "${VNC_PASSWORD:-}" ]; then
  VNC_PASSWD="$(command -v vncpasswd || command -v tigervncpasswd)"
  printf "%s\n" "$VNC_PASSWORD" | su - hermes -c "$VNC_PASSWD -f > /home/hermes/.vnc/passwd"
  chmod 600 /home/hermes/.vnc/passwd
  chown hermes:hermes /home/hermes/.vnc/passwd
fi

su - hermes -c "vncserver -kill :1" >/dev/null 2>&1 || true
rm -f /tmp/.X11-unix/X1 /tmp/.X1-lock
su - hermes -c "vncserver :1 -geometry ${VNC_RESOLUTION} -depth 24 -localhost no"

for i in $(seq 1 30); do
  if su - hermes -c "env HOME=/home/hermes DISPLAY=:1 XAUTHORITY=/home/hermes/.Xauthority /usr/bin/xdotool getdisplaygeometry" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "VNC display :1 did not become controllable in time" >&2
    exit 1
  fi
  sleep 1
done

websockify --web /usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &

cat <<EOF
============================================
Hermes Desktop is running
VNC:   vnc://localhost:${VNC_PORT}
noVNC: http://localhost:${NOVNC_PORT}/vnc.html
Display: ${DISPLAY}
Resolution: ${VNC_RESOLUTION}
============================================
EOF

while true; do
  if ! pgrep -u hermes -f 'Xtigervnc :1' >/dev/null 2>&1; then
    echo "VNC server process exited; keeping container alive for inspection" >&2
  fi
  sleep 30
done
