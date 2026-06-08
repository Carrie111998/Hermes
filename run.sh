#!/usr/bin/env bashio
set -e

# 1. Initialization
export HERMES_HOME="/data"
export INSTALL_DIR="/opt/hermes"
export HOME="/data"
cd "$INSTALL_DIR"

# Source helpers
source ./profile-init.sh
source ./nginx-render.sh

# 2. Configuration Mapping
bashio::log.info "Mapping configuration..."

export OPENROUTER_API_KEY=$(bashio::config 'openrouter_api_key')
export OPENAI_API_KEY=$(bashio::config 'openai_api_key')
export ANTHROPIC_API_KEY=$(bashio::config 'anthropic_api_key')
export ENABLE_DASHBOARD=$(bashio::config 'enable_dashboard')
export ENABLE_TERMINAL=$(bashio::config 'enable_terminal' 'true')
export ACCESS_PASSWORD=$(bashio::config 'access_password' '')
export HERMES_ALLOW_ROOT_GATEWAY=1

# Home Assistant integration
export HASS_URL="http://supervisor/core"
export HASS_TOKEN="${SUPERVISOR_TOKEN}"

# 3. Bootstrap Data Volume
mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}
mkdir -p "$HERMES_HOME"/.certs

# 4. Profile Resolution
resolve_profiles

# 5. Start Nginx with Loading Page
INGRESS_PORT=$(bashio::addon.ingress_port)
bashio::log.info "Starting Nginx on port ${INGRESS_PORT} with loading page..."

cat > /etc/nginx/nginx.conf << LOADCONF
worker_processes 1;
pid /var/run/nginx.pid;
error_log stderr warn;
events { worker_connections 64; }
http {
    server {
        listen ${INGRESS_PORT};
        location / { root ${INSTALL_DIR}; try_files /loading.html =404; }
    }
}
LOADCONF
nginx

# 6. SSL Certificates (Shared)
CERTS_DIR="$HERMES_HOME/.certs"
if [ ! -f "$CERTS_DIR/server.crt" ]; then
    bashio::log.info "Generating self-signed certificates..."
    openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -keyout "$CERTS_DIR/ca.key" -out "$CERTS_DIR/ca.crt" \
        -days 3650 -subj "/CN=Hermes Agent CA" 2>/dev/null
    openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -keyout "$CERTS_DIR/server.key" -out /tmp/server.csr \
        -subj "/CN=hermes-agent" 2>/dev/null
    openssl x509 -req -in /tmp/server.csr \
        -CA "$CERTS_DIR/ca.crt" -CAkey "$CERTS_DIR/ca.key" \
        -CAcreateserial -out "$CERTS_DIR/server.crt" \
        -days 3650 2>/dev/null
    rm -f /tmp/server.csr
fi

# 7. Start Services
bashio::log.info "Starting Hermes services..."

# Gateway (primary)
stdbuf -oL -eL hermes gateway > "$HERMES_HOME/logs/gateway.log" 2>&1 &
GATEWAY_PID=$!

# ttyd for Hermes
ttyd --port ${TTYD_HERMES_PORTS[0]} --interface 127.0.0.1 --base-path /hermes/ --writable \
    tmux -u new -A -s hermes hermes &

# ttyd for Terminal
if [ "$ENABLE_TERMINAL" = "true" ]; then
    ttyd --port ${TTYD_TERMINAL_PORTS[0]} --interface 127.0.0.1 --base-path /terminal/ --writable \
        tmux -u new -A -s terminal /bin/bash &
fi

# Dashboard
DASHBOARD_TOKENS=("")
if [ "$ENABLE_DASHBOARD" = "true" ]; then
    stdbuf -oL -eL hermes dashboard --host 127.0.0.1 --port ${DASHBOARD_PORTS[0]} --no-open --insecure > "$HERMES_HOME/logs/dashboard.log" 2>&1 &
    DASHBOARD_PID=$!

    # Wait for dashboard and extract token
    bashio::log.info "Waiting for Dashboard token..."
    for i in {1..30}; do
        DASHBOARD_TOKEN=$(curl -s http://127.0.0.1:${DASHBOARD_PORTS[0]}/ | sed -n 's/.*__HERMES_SESSION_TOKEN__="\([^"]*\)".*/\1/p' || true)
        if [ -n "$DASHBOARD_TOKEN" ]; then
            DASHBOARD_TOKENS=("$DASHBOARD_TOKEN")
            break;
        fi
        sleep 1
    done
fi

# 8. Final Nginx Configuration
bashio::log.info "Rendering final Nginx configuration..."

HERMES_VERSION=$(hermes --version | head -n1)
CERTS_DIR_ESC=$(echo "$CERTS_DIR" | sed 's/\//\\\//g')
AUTH_BASIC_ON="# auth disabled"

# Render config
cp nginx.conf.tpl /etc/nginx/nginx.conf
emit_upstreams | substitute_marker /etc/nginx/nginx.conf '%%UPSTREAMS%%'
emit_dashboard_maps | substitute_marker /etc/nginx/nginx.conf '%%DASHBOARD_MAPS%%'
emit_profile_locations ingress | substitute_marker /etc/nginx/nginx.conf '%%INGRESS_PROFILE_LOCATIONS%%'

sed -i \
    -e "s/%%INGRESS_PORT%%/${INGRESS_PORT}/g" \
    -e "s/%%CERTS_DIR%%/${CERTS_DIR_ESC}/g" \
    -e "s/%%INCLUDE_PORTS%%/# direct ports disabled/g" \
    /etc/nginx/nginx.conf

# Render Landing Page
ADDON_SLUG=$(hostname | tr '-' '_')
PROFILES_JSON='[{"name":"default","prefix":"","primary":true}]'
PROFILES_JSON_ESC=$(echo "$PROFILES_JSON" | sed 's/[\\/&]/\\&/g')

cp landing.html.tpl /var/www/landing.html
sed -i \
    -e "s/%%HERMES_VERSION%%/${HERMES_VERSION}/g" \
    -e "s/%%ADDON_SLUG%%/${ADDON_SLUG}/g" \
    -e "s/%%SHOW_TERMINAL%%/${ENABLE_TERMINAL}/g" \
    -e "s/%%SHOW_DASHBOARD%%/${ENABLE_DASHBOARD}/g" \
    -e "s/%%SHOW_API%%/true/g" \
    -e "s/%%PROFILES_JSON%%/${PROFILES_JSON_ESC}/g" \
    /var/www/landing.html

# Reload Nginx
nginx -s reload

bashio::log.info "All services started."

# 9. Wait/Monitor
while true; do
    if ! kill -0 $GATEWAY_PID 2>/dev/null; then
        bashio::log.error "Gateway died! Restarting in 5s..."
        sleep 5
        stdbuf -oL -eL hermes gateway >> "$HERMES_HOME/logs/gateway.log" 2>&1 &
        GATEWAY_PID=$!
    fi
    sleep 10
done
