#!/bin/bash
# shellcheck shell=bash

# Substitute a multi-line value into a template at a marker.
substitute_marker() {
    local file="$1" marker="$2"
    local tmp
    tmp="$(mktemp)"
    # Read the file and replace the marker line with stdin.
    # Note: Using sed with r /dev/stdin to avoid escaping issues with the replacement string.
    sed -e "/${marker}/{
        r /dev/stdin
        d
    }" "$file" > "$tmp" && mv "$tmp" "$file"
}

# Emit nginx upstream blocks for each profile.
emit_upstreams() {
    for i in "${!PROFILE_DIRS[@]}"; do
        local name="${PROFILE_NAMES[$i]}"
        cat << UPSTREAM
    upstream gateway_${name} { server 127.0.0.1:${API_PORTS[$i]}; }
    upstream dashboard_${name} { server 127.0.0.1:${DASHBOARD_PORTS[$i]}; }
    upstream ttyd_hermes_${name} { server 127.0.0.1:${TTYD_HERMES_PORTS[$i]}; }
    upstream ttyd_terminal_${name} { server 127.0.0.1:${TTYD_TERMINAL_PORTS[$i]}; }
UPSTREAM
    done
}

# Emit nginx map for dashboard session tokens.
emit_dashboard_maps() {
    cat << MAPHEAD
    # Map for dashboard API authentication
    map \$http_x_hermes_session_token \$hermes_authenticated {
        default 0;
MAPHEAD
    for i in "${!PROFILE_DIRS[@]}"; do
        [ -n "${DASHBOARD_TOKENS[$i]}" ] && echo "        \"${DASHBOARD_TOKENS[$i]}\" 1;"
    done
    echo "    }"
}

# Emit nginx location blocks for a profile.
emit_profile_locations() {
    local context="$1" # "ingress", "http", or "https"
    local i
    for i in "${!PROFILE_DIRS[@]}"; do
        local name="${PROFILE_NAMES[$i]}"
        local prefix="${PROFILE_PATH_PREFIX[$i]}"
        local auth_directive="%%AUTH_BASIC_ON%%"
        [ "$context" = "ingress" ] && auth_directive="auth_basic off;"

        cat << LOCATIONS
        # --- Profile: ${name} (${prefix:-primary}) ---
        location ${prefix}/hermes/ {
            ${auth_directive}
            proxy_pass http://ttyd_hermes_${name}/;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Host \$host;
        }

        location ${prefix}/terminal/ {
            ${auth_directive}
            proxy_pass http://ttyd_terminal_${name}/;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Host \$host;
        }

        location ${prefix}/dashboard/ {
            ${auth_directive}
            proxy_pass http://dashboard_${name}/;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
        }

        # Dashboard API (protected by session token)
        location ${prefix}/dashboard/api/ {
            if (\$hermes_authenticated = 0) {
                return 401;
            }
            proxy_pass http://dashboard_${name}/api/;
            proxy_set_header Host \$host;
        }

        # OpenAI-compatible API
        location ${prefix}/v1/ {
            auth_basic off;
            proxy_pass http://gateway_${name}/v1/;
            proxy_set_header Host \$host;
        }
LOCATIONS
    done
}
