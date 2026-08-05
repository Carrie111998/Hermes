#!/bin/bash
# Docker/Podman entrypoint: bootstrap config files into the mounted volume, then run hermes.
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"

# --- Privilege dropping via s6-setuidgid ---
# When started as root (the default for Docker, or fakeroot in rootless Podman),
# optionally remap the hermes user/group to match host-side ownership, fix volume
# permissions, then re-exec as hermes.
#
# Upstream's s6-overlay migration removed gosu from the image; the supervised
# boot path drops privileges with s6-setuidgid instead. This script runs as
# Railway's startCommand (invoked directly, outside s6's `with-contenv` PATH),
# so resolve the binary explicitly: it lives at /command/s6-setuidgid in the
# s6-overlay v3 layout and may not be on the default PATH here.
if [ "$(id -u)" = "0" ]; then
    if [ -n "$HERMES_UID" ] && [ "$HERMES_UID" != "$(id -u hermes)" ]; then
        echo "Changing hermes UID to $HERMES_UID"
        usermod -u "$HERMES_UID" hermes
    fi

    if [ -n "$HERMES_GID" ] && [ "$HERMES_GID" != "$(id -g hermes)" ]; then
        echo "Changing hermes GID to $HERMES_GID"
        # -o allows non-unique GID (e.g. macOS GID 20 "staff" may already exist
        # as "dialout" in the Debian-based container image)
        groupmod -o -g "$HERMES_GID" hermes 2>/dev/null || true
    fi

    # Fix ownership of the data volume. When HERMES_UID remaps the hermes user,
    # files created by previous runs (under the old UID) become inaccessible.
    # Always chown -R when UID was remapped; otherwise only if top-level is wrong.
    actual_hermes_uid=$(id -u hermes)
    needs_chown=false
    if [ -n "$HERMES_UID" ] && [ "$HERMES_UID" != "10000" ]; then
        needs_chown=true
    elif [ "$(stat -c %u "$HERMES_HOME" 2>/dev/null)" != "$actual_hermes_uid" ]; then
        needs_chown=true
    fi
    if [ "$needs_chown" = true ]; then
        echo "Fixing ownership of $HERMES_HOME to hermes ($actual_hermes_uid)"
        # In rootless Podman the container's "root" is mapped to an unprivileged
        # host UID — chown will fail.  That's fine: the volume is already owned
        # by the mapped user on the host side.
        chown -R hermes:hermes "$HERMES_HOME" 2>/dev/null || \
            echo "Warning: chown failed (rootless container?) — continuing anyway"
        # The .venv must also be re-chowned when UID is remapped, otherwise
        # lazy_deps.py cannot install platform packages (discord.py, etc.).
        chown -R hermes:hermes "$INSTALL_DIR/.venv" 2>/dev/null || \
            echo "Warning: chown .venv failed (rootless container?) — continuing anyway"
    fi

    # Always reset ownership of pairing data on every boot (mirrors upstream
    # stage2-hook.sh). `railway ssh` runs as root, so a `hermes pairing
    # approve …` from an SSH session writes 0600 root-owned approval files the
    # unprivileged gateway cannot read, silently leaving the approved user
    # unauthorized. The targeted chown above only runs when the top-level
    # $HERMES_HOME is mis-owned, so warm boots skip it — this makes a restart
    # self-heal. Tiny directory, negligible cost.
    if [ -d "$HERMES_HOME/platforms/pairing" ]; then
        chown -R hermes:hermes "$HERMES_HOME/platforms/pairing" 2>/dev/null || true
    fi
    # Legacy location (pre-consolidated layout).
    if [ -d "$HERMES_HOME/pairing" ]; then
        chown -R hermes:hermes "$HERMES_HOME/pairing" 2>/dev/null || true
    fi

    # Always ensure logs/gateways is hermes-owned (mirrors upstream
    # stage2-hook.sh, #45258). The mkdir below runs as root, so on a warm
    # volume with a hermes-owned $HERMES_HOME the targeted chown above is
    # skipped and a root-owned logs/gateways survives — the gateway then runs
    # under s6-setuidgid hermes and its mkdir fails with Permission denied.
    # Non-recursive: profile leaf dirs are each created by their own log/run
    # as hermes.
    # `! -L` mirrors upstream's refuse_symlinked_path guard: chown follows
    # symlinks, so a planted link would retarget the chown (CWE-59/367).
    if [ -d "$HERMES_HOME/logs/gateways" ] && [ ! -L "$HERMES_HOME/logs/gateways" ]; then
        chown hermes:hermes "$HERMES_HOME/logs/gateways" 2>/dev/null || true
    fi

    # Ensure config.yaml is readable by the hermes runtime user even if it was
    # edited on the host after initial ownership setup. Must run here (as root)
    # rather than after the privilege drop, otherwise a non-root caller like
    # `docker run -u $(id -u):$(id -g)` hits "Operation not permitted" (#15865).
    if [ -f "$HERMES_HOME/config.yaml" ]; then
        chown hermes:hermes "$HERMES_HOME/config.yaml" 2>/dev/null || true
        chmod 640 "$HERMES_HOME/config.yaml" 2>/dev/null || true
    fi

    echo "Dropping root privileges"
    # Prefer s6-setuidgid (shipped by the s6-overlay image); fall back to its
    # canonical /command path if PATH doesn't include the s6 symlink dir.
    setuidgid="$(command -v s6-setuidgid || true)"
    [ -z "$setuidgid" ] && [ -x /command/s6-setuidgid ] && setuidgid=/command/s6-setuidgid
    if [ -n "$setuidgid" ]; then
        exec "$setuidgid" hermes "$0" "$@"
    fi
    # Last-resort fallback if s6-setuidgid is somehow unavailable.
    exec su hermes -c "$(printf '%q ' "$0" "$@")"
fi

# --- Running as hermes from here ---
# HOME is inherited as /root from the container's root start context, but we
# now run as the unprivileged hermes user, which cannot write under /root.
# Several runtime paths resolve via $HOME — notably the gateway platform locks
# at $XDG_STATE_HOME/hermes/gateway-locks, defaulting to ~/.local/state
# (see gateway/status.py). Point HOME at the writable, persistent data dir so
# those paths land on the volume. Mirrors upstream's s6 main-wrapper.sh, which
# does `export HOME=/opt/data` for exactly this reason (PR #33481).
export HOME="${HERMES_HOME}"

source "${INSTALL_DIR}/.venv/bin/activate"

# Let the GitHub CLI (gh) authenticate non-interactively from the same PAT the
# GitHub MCP connector uses. gh reads GH_TOKEN/GITHUB_TOKEN, not
# GITHUB_PERSONAL_ACCESS_TOKEN, so bridge it here (only when unset). Inherited by
# the gateway/dashboard and every profile agent they spawn.
if [ -n "$GITHUB_PERSONAL_ACCESS_TOKEN" ] && [ -z "$GH_TOKEN" ] && [ -z "$GITHUB_TOKEN" ]; then
    export GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN"
fi

# Create essential directory structure.  Cache and platform directories
# (cache/images, cache/audio, platforms/whatsapp, etc.) are created on
# demand by the application — don't pre-create them here so new installs
# get the consolidated layout from get_hermes_dir().
# The "home/" subdirectory is a per-profile HOME for subprocesses (git,
# ssh, gh, npm …).  Without it those tools write to /root which is
# ephemeral and shared across profiles.  See issue #4426.
mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}

# .env — on Railway, use an empty .env so Railway-injected env vars are not
# overwritten by stale values from .env.example (load_dotenv override=True).
if [ -n "$RAILWAY_ENVIRONMENT" ]; then
    : > "$HERMES_HOME/.env"
elif [ ! -f "$HERMES_HOME/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
fi

# config.yaml — on Railway, always use the Railway-optimized config
if [ -n "$RAILWAY_ENVIRONMENT" ] && [ -f "$INSTALL_DIR/docker/railway-config.yaml" ]; then
    cp "$INSTALL_DIR/docker/railway-config.yaml" "$HERMES_HOME/config.yaml"
elif [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
fi

# SOUL.md — le profil par défaut EST l'orchestrateur ; sa persona est versionnée
# dans docker/SOUL.md. Sur Railway, on la rafraîchit à CHAQUE boot (comme config.yaml
# ci-dessus) : sinon un SOUL.md obsolète sur le volume persiste, et le runtime
# (hermes_cli/config.py:_seed_default_soul) reseede la persona Nous stock dès qu'il
# est absent — course qui écrasait l'orchestrateur. L'entrypoint tourne AVANT le
# gateway, donc écrire ici est race-free (_seed_default_soul ne touche pas un SOUL
# personnalisé). Hors Railway, on seede seulement si absent (éditions locales préservées).
if [ -f "$INSTALL_DIR/docker/SOUL.md" ] && { [ -n "$RAILWAY_ENVIRONMENT" ] || [ ! -f "$HERMES_HOME/SOUL.md" ]; }; then
    cp "$INSTALL_DIR/docker/SOUL.md" "$HERMES_HOME/SOUL.md"
fi

# --- Named profiles: seed dedicated agents on the volume (idempotent) --------
# "web-design" and "web-dev" are full Hermes profiles living under
# $HERMES_HOME/profiles/<name>/. The dashboard (which the Desktop app connects
# to) is machine-level with a global profile switcher, so simply creating these
# on the volume surfaces them as separate agents — no extra gateway/token.
#
# Created once via the official `hermes profile create` path so the structure is
# valid (profile.yaml, skills, wrapper), then SOUL.md/config.yaml are overlaid
# from the committed templates under docker/profiles/<name>/. On Railway BOTH
# config.yaml and SOUL.md are always refreshed (mirrors the default-profile
# behavior above): the repo templates are the source of truth — seed-only SOUL.md
# left web-design/seo-geo running the stock Nous persona (513-byte stub predating
# the templates) forever. Off Railway both stay seed-only so local edits survive.
# The shared kanban board at
# $HERMES_HOME/kanban.db lets the profiles delegate work to each other.
for _p in web-design web-dev wmh-offers seo-geo; do
    _pdir="$HERMES_HOME/profiles/$_p"
    _tmpl="$INSTALL_DIR/docker/profiles/$_p"
    if [ ! -d "$_pdir" ]; then
        if [ "$_p" = "web-design" ]; then
            _desc="Design & intégration web (maquettes, UI, responsive)"
        elif [ "$_p" = "wmh-offers" ]; then
            _desc="Propositions digitales — pipeline offres, devis, PPTX WMH"
        elif [ "$_p" = "seo-geo" ]; then
            _desc="Suivi SEO & GEO — Search Console, WordPress, visibilité IA (tanorient.com)"
        else
            _desc="Dev full-stack web — GitHub, Supabase, Netlify"
        fi
        echo "[entrypoint] creating profile: $_p"
        hermes profile create "$_p" --description "$_desc" \
            || echo "[entrypoint] WARNING: 'hermes profile create $_p' failed"
    fi
    # Overlay templates only if the profile dir exists (create may have failed);
    # guard so a missing dir never aborts boot under `set -e`.
    if [ -d "$_pdir" ]; then
        if [ -f "$_tmpl/SOUL.md" ] && { [ -n "$RAILWAY_ENVIRONMENT" ] || [ ! -f "$_pdir/SOUL.md" ]; }; then
            cp "$_tmpl/SOUL.md" "$_pdir/SOUL.md" || true
        fi
        if [ -f "$_tmpl/config.yaml" ] && { [ -n "$RAILWAY_ENVIRONMENT" ] || [ ! -f "$_pdir/config.yaml" ]; }; then
            cp "$_tmpl/config.yaml" "$_pdir/config.yaml" || true
        fi
        # Refresh the google-workspace skill from the bundled (fixed) copy. The
        # version seeded at profile-create time routes through gws, which cannot
        # parse Hermes' token format; the bundled copy now defaults to the Python
        # google-api path that works. Refresh on Railway (or seed if missing).
        _gwsk="$INSTALL_DIR/skills/productivity/google-workspace"
        if [ -d "$_gwsk" ] && { [ -n "$RAILWAY_ENVIRONMENT" ] || [ ! -d "$_pdir/skills/productivity/google-workspace" ]; }; then
            mkdir -p "$_pdir/skills/productivity"
            rm -rf "$_pdir/skills/productivity/google-workspace"
            cp -a "$_gwsk" "$_pdir/skills/productivity/google-workspace" 2>/dev/null || true
        fi
        # Seed profile-specific skills committed under docker/profiles/<p>/skills/
        # (e.g. web-design-charter) into the profile. Refresh on Railway, else
        # seed when missing, so profiles pick up updates on redeploy.
        if [ -d "$_tmpl/skills" ]; then
            for _sk in "$_tmpl"/skills/*/; do
                [ -d "$_sk" ] || continue
                _skname=$(basename "$_sk")
                if [ -n "$RAILWAY_ENVIRONMENT" ] || [ ! -d "$_pdir/skills/$_skname" ]; then
                    mkdir -p "$_pdir/skills"
                    rm -rf "$_pdir/skills/$_skname"
                    cp -a "$_sk" "$_pdir/skills/$_skname" 2>/dev/null || true
                fi
            done
        fi
    fi
done

# auth.json: bootstrap from env on first boot only.  Used by orchestrators
# (e.g. provisioning a Hermes VPS from an account-management service) that
# need to seed the OAuth refresh credential non-interactively, instead of
# walking the user through `hermes setup` + the device-flow login dance.
# Subsequent token rotations write back to the same file, which lives on a
# persistent volume — so this env var is consumed exactly once at first
# boot.  The `[ ! -f ... ]` guard is critical: without it, a container
# restart would clobber a rotated refresh token with the now-stale value
# the orchestrator originally seeded.
if [ ! -f "$HERMES_HOME/auth.json" ] && [ -n "$HERMES_AUTH_JSON_BOOTSTRAP" ]; then
    printf '%s' "$HERMES_AUTH_JSON_BOOTSTRAP" > "$HERMES_HOME/auth.json"
    chmod 600 "$HERMES_HOME/auth.json"
fi

# auth.json: re-seed a TERMINALLY-DEAD Nous bootstrap session (self-heal,
# mirrors upstream stage2-hook.sh). The create-only guard above refuses to
# clobber an existing auth.json, so a terminal invalid_grant leaves the
# container unauthenticated across restarts. HERMES_AUTH_JSON_REBOOTSTRAP
# (distinct from *_BOOTSTRAP) swaps ONLY the providers.nous entry, and ONLY
# when the on-disk entry is provably terminal — safe to leave set. We are
# already running as hermes here (past the privilege drop), so no setuidgid.
if [ -f "$HERMES_HOME/auth.json" ] && [ -n "${HERMES_AUTH_JSON_REBOOTSTRAP:-}" ] \
    && [ ! -L "$HERMES_HOME/auth.json" ]; then
    "$INSTALL_DIR/.venv/bin/python" \
        "$INSTALL_DIR/scripts/docker_rebootstrap_nous_session.py" \
        "$HERMES_HOME/auth.json" \
        || echo "[entrypoint] Warning: docker_rebootstrap_nous_session.py failed; continuing"
fi

# Sync bundled skills (manifest-based so user edits are preserved)
if [ -d "$INSTALL_DIR/skills" ]; then
    python3 "$INSTALL_DIR/tools/skills_sync.py"
fi

# --- LLM-wiki git sync (idempotent, best-effort) -----------------------------
# The wiki at $WIKI_PATH is shared with other agents (Claude Cowork on the Mac)
# through a plain git remote (WIKI_GIT_REMOTE — see .env.railway). First boot
# with the var set adopts the existing tree into a fresh repo; later boots
# commit local drift, rebase on the remote, and push. Everything runs best-effort
# in a subshell so a git/network failure never blocks boot (set -e is active).
# The credential helper reads the PAT from the environment at run time, so no
# token is ever written to the volume; the helper also serves the agent's own
# `git push` after wiki edits (SOUL.md instructs it to publish).
if [ -n "${WIKI_PATH:-}" ] && [ -d "${WIKI_PATH:-}" ] && [ -n "${WIKI_GIT_REMOTE:-}" ] \
    && [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ] && command -v git >/dev/null 2>&1; then
    (
        set +e
        cd "$WIKI_PATH" || exit 0
        export GIT_TERMINAL_PROMPT=0
        if [ ! -d .git ]; then
            echo "[entrypoint] wiki: initializing git repo for $WIKI_GIT_REMOTE"
            git init -q -b main
        fi
        git config user.name "hermes-agent"
        git config user.email "hermes-agent@noreply.railway.app"
        git config credential.helper \
            '!f() { echo username=x-access-token; echo "password=${GITHUB_PERSONAL_ACCESS_TOKEN}"; }; f'
        if git remote get-url origin >/dev/null 2>&1; then
            git remote set-url origin "$WIKI_GIT_REMOTE"
        else
            git remote add origin "$WIKI_GIT_REMOTE"
        fi
        # The hot files are append-only; union-merge them so the two writers
        # (this agent + Cowork) never conflict on concurrent appends.
        if [ ! -f .gitattributes ]; then
            printf 'log.md merge=union\n_meta/inbox.md merge=union\n' > .gitattributes
        fi
        git add -A
        git diff --cached --quiet || git commit -q -m "wiki: boot sync"
        git fetch -q origin main 2>/dev/null
        if git rev-parse -q --verify origin/main >/dev/null 2>&1; then
            if ! git rebase --autostash origin/main >/dev/null 2>&1; then
                git rebase --abort >/dev/null 2>&1
                echo "[entrypoint] wiki: WARNING rebase conflict — keeping local state, resolve manually"
            fi
        fi
        if git push -q -u origin main >/dev/null 2>&1; then
            echo "[entrypoint] wiki: synced with $WIKI_GIT_REMOTE"
        else
            echo "[entrypoint] wiki: WARNING push failed — will retry on next boot"
        fi
    )
fi

# Google OAuth — decode base64 credentials from env vars into files.
# Always overwrite so Railway env var updates take effect on redeploy.
# Set GOOGLE_TOKEN_B64 and GOOGLE_CLIENT_SECRET_B64 in Railway dashboard.
# Generate locally with:
#   cat ~/.hermes/google_token.json | base64 | tr -d '\n'
#   cat ~/.hermes/google_client_secret.json | base64 | tr -d '\n'
if [ -n "$GOOGLE_TOKEN_B64" ]; then
    echo "$GOOGLE_TOKEN_B64" | base64 -d > "$HERMES_HOME/google_token.json"
    echo "[entrypoint] Google OAuth: decoded token → $HERMES_HOME/google_token.json"
fi
if [ -n "$GOOGLE_CLIENT_SECRET_B64" ]; then
    echo "$GOOGLE_CLIENT_SECRET_B64" | base64 -d > "$HERMES_HOME/google_client_secret.json"
    echo "[entrypoint] Google OAuth: decoded client secret → $HERMES_HOME/google_client_secret.json"
fi

# Share Google credentials with the named profiles. The google-workspace skill
# reads the token at get_hermes_home()/google_token.json, which for a profile is
# profiles/<name>/google_token.json — so without this, web-dev/web-design cannot
# access Google Workspace / Drive. Symlink (not copy) so the token the skill
# refreshes and writes back stays a single shared file. GWS_CONFIG_DIR/gws CLI is
# already shared via the exported GOOGLE_WORKSPACE_CLI_CONFIG_DIR below.
if [ -f "$HERMES_HOME/google_token.json" ] && [ -d "$HERMES_HOME/profiles" ]; then
    for _pd in "$HERMES_HOME"/profiles/*/; do
        [ -d "$_pd" ] || continue
        ln -sf "$HERMES_HOME/google_token.json" "${_pd}google_token.json" 2>/dev/null || true
        if [ -f "$HERMES_HOME/google_client_secret.json" ]; then
            ln -sf "$HERMES_HOME/google_client_secret.json" "${_pd}google_client_secret.json" 2>/dev/null || true
        fi
        echo "[entrypoint] Google OAuth: linked credentials into ${_pd}"
    done
fi

# gws CLI — set up config directory and bridge credentials if available
GWS_CONFIG_DIR="$HERMES_HOME/gws-config"
mkdir -p "$GWS_CONFIG_DIR"
export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="$GWS_CONFIG_DIR"

# If gws credentials file is set via env var, use it directly
if [ -n "$GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE" ]; then
    echo "[entrypoint] gws CLI: using credentials from GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"
# Otherwise, bridge from existing google-workspace Python skill token
elif [ -f "$HERMES_HOME/google_token.json" ]; then
    BRIDGE_SCRIPT="$INSTALL_DIR/skills/productivity/gws-cli/scripts/bridge_auth.py"
    if [ -f "$BRIDGE_SCRIPT" ]; then
        python3 "$BRIDGE_SCRIPT" --bridge 2>/dev/null && \
            export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE="$GWS_CONFIG_DIR/credentials.json" && \
            echo "[entrypoint] gws CLI: bridged credentials from google-workspace skill"
    fi
fi

# Firebase — service-account auth for the Firebase CLI (deploys + hosting preview
# channels). The default profile already deploys CRON reports via a service
# account at $HERMES_HOME/firebase-service-account.json; export
# GOOGLE_APPLICATION_CREDENTIALS so `firebase` authenticates non-interactively
# for ALL profiles (incl. web-dev) without a per-command prefix. For durability
# across a volume reset, optionally seed the key from FIREBASE_SERVICE_ACCOUNT_B64
# (base64 of the JSON) in the Railway dashboard — generate with:
#   cat ~/.hermes/firebase-service-account.json | base64 | tr -d '\n'
FIREBASE_SA="$HERMES_HOME/firebase-service-account.json"
if [ -n "$FIREBASE_SERVICE_ACCOUNT_B64" ]; then
    echo "$FIREBASE_SERVICE_ACCOUNT_B64" | base64 -d > "$FIREBASE_SA"
    chmod 600 "$FIREBASE_SA"
    echo "[entrypoint] Firebase: decoded service account → $FIREBASE_SA"
fi
if [ -f "$FIREBASE_SA" ]; then
    export GOOGLE_APPLICATION_CREDENTIALS="$FIREBASE_SA"
    echo "[entrypoint] Firebase: GOOGLE_APPLICATION_CREDENTIALS → $FIREBASE_SA"
else
    echo "[entrypoint] Firebase: no service account (set FIREBASE_SERVICE_ACCOUNT_B64 to enable)"
fi

# Google Search Console — service-account key for the gsc MCP server of the
# seo-geo profile (docker/profiles/seo-geo/config.yaml points its
# GOOGLE_APPLICATION_CREDENTIALS at this file; the stdio MCP subprocess gets
# the path via its own env: block, so the firebase GOOGLE_APPLICATION_CREDENTIALS
# export above is not clobbered). Seed from GSC_SERVICE_ACCOUNT_B64 (base64 of
# the JSON key) in the Railway dashboard — generate with:
#   cat gsc-service-account.json | base64 | tr -d '\n'
GSC_SA="$HERMES_HOME/gsc-service-account.json"
if [ -n "$GSC_SERVICE_ACCOUNT_B64" ]; then
    echo "$GSC_SERVICE_ACCOUNT_B64" | base64 -d > "$GSC_SA"
    chmod 600 "$GSC_SA"
    echo "[entrypoint] GSC: decoded service account → $GSC_SA"
elif [ ! -f "$GSC_SA" ]; then
    echo "[entrypoint] GSC: no service account (set GSC_SERVICE_ACCOUNT_B64 to enable the gsc MCP)"
fi

# Claude Code CLI — headless coding delegate for the web-dev profile.
# Auth is non-interactive via ANTHROPIC_API_KEY (Railway var, already visible
# to the whole container). Pin its config/state dir onto the volume so
# onboarding state and settings survive redeploys (the image filesystem is
# ephemeral; /opt/data persists). Shared across profiles — same API key.
export CLAUDE_CONFIG_DIR="$HERMES_HOME/claude-code"
mkdir -p "$CLAUDE_CONFIG_DIR"
echo "[entrypoint] Claude Code: CLAUDE_CONFIG_DIR → $CLAUDE_CONFIG_DIR (ANTHROPIC_API_KEY $([ -n "$ANTHROPIC_API_KEY" ] && echo set || echo MISSING))"

# Debug: verify Google OAuth files exist
echo "[entrypoint] google_token.json: $([ -f "$HERMES_HOME/google_token.json" ] && echo "EXISTS ($(wc -c < "$HERMES_HOME/google_token.json") bytes)" || echo "MISSING")"
echo "[entrypoint] google_client_secret.json: $([ -f "$HERMES_HOME/google_client_secret.json" ] && echo "EXISTS" || echo "MISSING")"

# Debug: verify env vars are visible
echo "[entrypoint] DISCORD_ALLOWED_USERS=${DISCORD_ALLOWED_USERS:-EMPTY}"
echo "[entrypoint] DISCORD_ALLOW_ALL_USERS=${DISCORD_ALLOW_ALL_USERS:-EMPTY}"
echo "[entrypoint] ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:+set}"
echo "[entrypoint] .env contents:"
cat "$HERMES_HOME/.env"
echo "[entrypoint] --- end .env ---"

# Cron ticker for secondary profiles (Railway runs a single foreground gateway
# on the default profile; without gateway.multiplex_profiles the built-in
# ticker only owns the default store — upstream #69377 — so named-profile cron
# jobs would show "scheduled" forever without this). Cheap side-loop: tick each
# listed profile's due jobs once a minute via `hermes -p <p> cron tick`.
# Delivery works without live adapters (standalone bot-token HTTP path in
# cron/scheduler.py:_deliver_result, incl. MEDIA: attachments). Railway-only:
# under s6/docker the per-profile gateway services own their own tickers.
if [ -n "$RAILWAY_ENVIRONMENT" ]; then
    (
        while true; do
            sleep 60
            for _cp in seo-geo; do
                _cplog="$HERMES_HOME/profiles/$_cp/logs/cron-tick.log"
                # crude rotation: keep the log under ~1 MB
                if [ -f "$_cplog" ] && [ "$(wc -c < "$_cplog")" -gt 1048576 ]; then
                    tail -c 262144 "$_cplog" > "$_cplog.tmp" && mv "$_cplog.tmp" "$_cplog"
                fi
                hermes -p "$_cp" cron tick >> "$_cplog" 2>&1 || true
            done
        done
    ) &
    echo "[entrypoint] cron side-ticker started for profiles: seo-geo"
fi

# Optionally start `hermes dashboard` as a side-process.
#
# This is what the Hermes Desktop app connects to as a "Remote gateway"
# (Settings > Gateway): it serves the same REST + WebSocket API the desktop
# shell speaks, so a long-running agent can stay here on Railway while you
# chat from a native window.
#
# Toggled by HERMES_DASHBOARD=1 (also accepts "true"/"yes", case-insensitive).
# Host/port/TUI can be overridden via:
#   HERMES_DASHBOARD_HOST  (default 0.0.0.0 — exposed outside the container)
#   HERMES_DASHBOARD_PORT  (default: Railway's $PORT when set, else 9119 —
#                           binding to $PORT lets the generated
#                           *.up.railway.app domain route to the dashboard
#                           without a manual target-port setting)
#   HERMES_DASHBOARD_TUI   (already honored by `hermes dashboard` itself)
#
# Auth posture on a non-loopback bind — REQUIRED before exposing this on a
# public URL, because the dashboard surfaces your API keys (/api/env,
# /api/config). The dashboard's OAuth gate engages on a non-loopback bind
# *unless* --insecure is passed; we pick the gate from what's configured,
# in priority order:
#   1. HERMES_DASHBOARD_OAUTH_CLIENT_ID set  -> Nous Portal OAuth gate.
#      The recommended path: "Sign in with Nous Research" from the Desktop
#      app. Register the client once with `hermes dashboard register` or via
#      the Portal /local-dashboards page; the value has shape agent:{id}.
#   2. HERMES_DASHBOARD_BASIC_AUTH_USERNAME (+ _PASSWORD or _PASSWORD_HASH)
#      set -> username/password gate. Keep behind a VPN; not for a public URL.
#   3. HERMES_DASHBOARD_INSECURE truthy      -> legacy --insecure escape hatch
#      (NO auth gate; protected only by the static session token). Opt-in.
#   4. none of the above                     -> do NOT start the dashboard;
#      print how to configure it. Fail-safe: never expose an unauthenticated
#      dashboard, and never crash-loop the (foreground) gateway.
#
# The dashboard is a long-lived server.  We background it *before* the final
# `exec hermes "$@"` so the user's chosen foreground command (chat, gateway,
# sleep infinity, …) remains PID-of-interest for the container runtime.  When
# the container stops the whole process tree is torn down, so no explicit
# cleanup is needed.
case "${HERMES_DASHBOARD:-}" in
    1|true|TRUE|True|yes|YES|Yes)
        dash_host="${HERMES_DASHBOARD_HOST:-0.0.0.0}"
        dash_port="${HERMES_DASHBOARD_PORT:-${PORT:-9119}}"
        dash_args=(--host "$dash_host" --port "$dash_port" --no-open)

        dash_start=1
        if [ "$dash_host" != "127.0.0.1" ] && [ "$dash_host" != "localhost" ]; then
            # Non-loopback bind: pick an auth posture (see comment above).
            if [ -n "${HERMES_DASHBOARD_OAUTH_CLIENT_ID:-}" ]; then
                # Behind Railway's TLS-terminating proxy the OAuth redirect_uri
                # must be the public https URL.  Derive it from Railway's
                # public domain when the operator hasn't set it explicitly.
                if [ -z "${HERMES_DASHBOARD_PUBLIC_URL:-}" ] && [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
                    export HERMES_DASHBOARD_PUBLIC_URL="https://${RAILWAY_PUBLIC_DOMAIN}"
                fi
                echo "[entrypoint] dashboard: Nous Portal OAuth gate ENGAGED (client ${HERMES_DASHBOARD_OAUTH_CLIENT_ID})"
                echo "[entrypoint] dashboard: public URL = ${HERMES_DASHBOARD_PUBLIC_URL:-<derived from forwarded headers>}"
            elif [ -n "${HERMES_DASHBOARD_BASIC_AUTH_USERNAME:-}" ] && \
                 { [ -n "${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:-}" ] || [ -n "${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH:-}" ]; }; then
                echo "[entrypoint] dashboard: username/password gate ENGAGED (user ${HERMES_DASHBOARD_BASIC_AUTH_USERNAME})"
            else
                case "${HERMES_DASHBOARD_INSECURE:-}" in
                    1|true|TRUE|True|yes|YES|Yes)
                        dash_args+=(--insecure)
                        echo "[entrypoint] WARNING: dashboard starting with --insecure — NO auth gate." >&2
                        echo "[entrypoint]          Anyone who reaches ${dash_host}:${dash_port} with the session" >&2
                        echo "[entrypoint]          token can read your API keys.  Prefer OAuth: set" >&2
                        echo "[entrypoint]          HERMES_DASHBOARD_OAUTH_CLIENT_ID (see .env.railway)." >&2
                        ;;
                    *)
                        dash_start=0
                        echo "[entrypoint] dashboard NOT started: a non-loopback bind ($dash_host) needs an auth gate." >&2
                        echo "[entrypoint]   Recommended: set HERMES_DASHBOARD_OAUTH_CLIENT_ID (agent:{id} from" >&2
                        echo "[entrypoint]   'hermes dashboard register' or the Portal /local-dashboards page)." >&2
                        echo "[entrypoint]   Or set HERMES_DASHBOARD_BASIC_AUTH_USERNAME + _PASSWORD for a password." >&2
                        echo "[entrypoint]   Or set HERMES_DASHBOARD_INSECURE=1 to opt into the token-only escape" >&2
                        echo "[entrypoint]   hatch (NOT recommended on a public URL)." >&2
                        ;;
                esac
            fi
        fi

        if [ "$dash_start" = "1" ]; then
            echo "Starting hermes dashboard on ${dash_host}:${dash_port} (background)"
            # Prefix dashboard output so it's distinguishable from the main
            # process in `docker logs`.  stdbuf keeps the pipe line-buffered.
            (
                stdbuf -oL -eL hermes dashboard "${dash_args[@]}" 2>&1 \
                    | sed -u 's/^/[dashboard] /'
            ) &
        fi
        ;;
esac

# Final exec: two supported invocation patterns.
#
#   docker run <image>                 -> exec `hermes` with no args (legacy default)
#   docker run <image> chat -q "..."   -> exec `hermes chat -q "..."` (legacy wrap)
#   docker run <image> sleep infinity  -> exec `sleep infinity` directly
#   docker run <image> bash            -> exec `bash` directly
#
# If the first positional arg resolves to an executable on PATH, we assume the
# caller wants to run it directly (needed by the launcher which runs long-lived
# `sleep infinity` sandbox containers — see tools/environments/docker.py).
# Otherwise we treat the args as a hermes subcommand and wrap with `hermes`,
# preserving the documented `docker run <image> <subcommand>` behavior.
if [ $# -gt 0 ] && command -v "$1" >/dev/null 2>&1; then
    exec "$@"
fi
exec hermes "$@"
