#!/bin/bash
# easypanel-entrypoint.sh — Lightweight entrypoint for Easypanel deployment
# Seeds data dir (config, SOUL.md, memories, git config), then exec's the gateway via gosu privilege drop.
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"
DATA_SEED="/opt/data-seed"
MEMORIES_SEED="/opt/memories-seed"

# --- Bootstrap as root, then drop to hermes ---
if [ "$(id -u)" = 0 ]; then
    # Fix ownership of data volume
    chown hermes:hermes "$HERMES_HOME" 2>/dev/null || true

    # Seed config on first boot (preserve existing files from volume mounts)
    if [ ! -f "$HERMES_HOME/.env" ] && [ -f "$INSTALL_DIR/.env.example" ]; then
        cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
        chown hermes:hermes "$HERMES_HOME/.env"
        chmod 600 "$HERMES_HOME/.env"
    fi
    # Always deploy the Easypanel-optimized config so model/provider
    # changes take effect on redeploy (the persistent volume may have
    # a stale config from a previous image).  User customizations
    # should go in the overlay ($DATA_SEED/config.yaml) which takes
    # precedence; the base config is overwritten every boot.
    if [ -f "$DATA_SEED/config.yaml" ]; then
        cp "$DATA_SEED/config.yaml" "$HERMES_HOME/config.yaml"
    elif [ -f "$INSTALL_DIR/docker/easypanel-config.yaml" ]; then
        cp "$INSTALL_DIR/docker/easypanel-config.yaml" "$HERMES_HOME/config.yaml"
    fi
    chown hermes:hermes "$HERMES_HOME/config.yaml" 2>/dev/null || true
    chmod 640 "$HERMES_HOME/config.yaml" 2>/dev/null || true
    if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
        # Seed from overlay first, then fallback to inline default
        if [ -f "$DATA_SEED/SOUL.md" ]; then
            cp "$DATA_SEED/SOUL.md" "$HERMES_HOME/SOUL.md"
        else
            printf 'You are Hermes Agent, an intelligent AI assistant. You are helpful, knowledgeable, and direct.\n' > "$HERMES_HOME/SOUL.md"
        fi
        chown hermes:hermes "$HERMES_HOME/SOUL.md"
    fi

    # Seed memories (MEMORY.md and USER.md) from image seed directory
    # Only seed if files don't already exist on the persistent volume
    mkdir -p "$HERMES_HOME/memories"
    if [ ! -f "$HERMES_HOME/memories/MEMORY.md" ] && [ -f "$MEMORIES_SEED/MEMORY.md" ]; then
        cp "$MEMORIES_SEED/MEMORY.md" "$HERMES_HOME/memories/MEMORY.md"
    fi
    if [ ! -f "$HERMES_HOME/memories/USER.md" ] && [ -f "$MEMORIES_SEED/USER.md" ]; then
        cp "$MEMORIES_SEED/USER.md" "$HERMES_HOME/memories/USER.md"
    fi

    # Configure git for the hermes user so the agent can clone private repos
    if [ -n "${GITHUB_PAT:-}" ]; then
        # Store PAT in git credential helper for HTTPS clone access
        HERMES_GITCONFIG="$HERMES_HOME/.gitconfig"
        if [ ! -f "$HERMES_GITCONFIG" ]; then
            printf '[credential]\n\thelper = store\n[user]\n\tname = Hermes Agent\n\temail = hermes@tad.do\n[core]\n\tautocrlf = input\n' > "$HERMES_GITCONFIG"
            # Write credential entry for GitHub
            printf 'https://tadtaxiadvertising:%s@github.com\n' "$GITHUB_PAT" > "$HERMES_HOME/.git-credentials"
            chown hermes:hermes "$HERMES_HOME/.gitconfig" "$HERMES_HOME/.git-credentials"
            chmod 600 "$HERMES_HOME/.git-credentials"
        fi
    fi

    # Create runtime subdirectories owned by hermes
    mkdir -p \
        "$HERMES_HOME/cron" \
        "$HERMES_HOME/sessions" \
        "$HERMES_HOME/logs" \
        "$HERMES_HOME/hooks" \
        "$HERMES_HOME/memories" \
        "$HERMES_HOME/skills" \
        "$HERMES_HOME/lazy-packages" \
        "$HERMES_HOME/platforms/pairing" \
        "$HERMES_HOME/cache"
    chown -R hermes:hermes \
        "$HERMES_HOME/cron" \
        "$HERMES_HOME/sessions" \
        "$HERMES_HOME/logs" \
        "$HERMES_HOME/hooks" \
        "$HERMES_HOME/memories" \
        "$HERMES_HOME/skills" \
        "$HERMES_HOME/lazy-packages" \
        "$HERMES_HOME/platforms/pairing" \
        "$HERMES_HOME/cache" 2>/dev/null || true

    # Drop to hermes user via gosu (lightweight su-exec alternative)
    exec gosu hermes "$@"
else
    exec "$@"
fi
