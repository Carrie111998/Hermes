#!/bin/bash
# ============================================================================
# RecursiveIntell Hermes Installer
# ============================================================================
# Installs Hermes Agent from the RecursiveIntell fork with Rust acceleration
# (llm-pipeline, context-governor, poly-kv) pre-configured and default-on.
#
# Usage:
#   curl -fsSL https://recursiveintell.com/hermes/install.sh | bash
#
# Or:
#   curl -fsSL https://raw.githubusercontent.com/RecursiveIntell/hermes-agent/main/install.sh | bash
#
# Options:
#   --skip-rust       Skip PyO3 wheel installation (Python-only fallback)
#   --no-venv         Use system Python instead of uv-managed venv
#   --skip-setup      Skip post-install setup wizard
#   --branch NAME     Install from a specific branch (default: main)
#   --help            Show this message
# ============================================================================

set -e

# ── Environment guard ───────────────────────────────────────────────
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH during install"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME during install"
    unset PYTHONHOME
fi
export UV_NO_CONFIG=1

# ── Colors ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Configuration ───────────────────────────────────────────────────
REPO_URL="https://github.com/RecursiveIntell/hermes-agent.git"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALL_DIR="${HERMES_INSTALL_DIR:-}"
PYTHON_VERSION="3.11"
BRANCH="main"

# Options
USE_VENV=true
RUN_SETUP=true
SKIP_RUST=false

# ── Logging ─────────────────────────────────────────────────────────
log()   { echo -e "${GREEN}[RI]${NC} $1"; }
warn()  { echo -e "${YELLOW}[RI]${NC} $1"; }
err()   { echo -e "${RED}[RI]${NC} $1"; }
info()  { echo -e "${BLUE}[RI]${NC} $1"; }
step()  { echo -e "${CYAN}→${NC} $1"; }
ok()    { echo -e "  ${GREEN}✓${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; }

# ── Banner ──────────────────────────────────────────────────────────
banner() {
    echo
    echo -e "${BOLD}${CYAN}  ╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}  ║${NC}  ${BOLD}RecursiveIntell Hermes${NC} — Rust-accelerated AI agent  ${BOLD}${CYAN}║${NC}"
    echo -e "${BOLD}${CYAN}  ║${NC}  llm-pipeline · context-governor · poly-kv     ${BOLD}${CYAN}║${NC}"
    echo -e "${BOLD}${CYAN}  ╚══════════════════════════════════════════════╝${NC}"
    echo
}

# ── Help ────────────────────────────────────────────────────────────
show_help() {
    echo "RecursiveIntell Hermes Installer"
    echo
    echo "Installs Hermes Agent with Rust acceleration pre-configured."
    echo
    echo "Usage:"
    echo "  curl -fsSL https://recursiveintell.com/hermes/install.sh | bash"
    echo
    echo "Options:"
    echo "  --skip-rust     Skip PyO3 wheel installation"
    echo "  --no-venv       Use system Python instead of uv venv"
    echo "  --skip-setup    Skip post-install setup wizard"
    echo "  --branch NAME   Install from a specific branch (default: main)"
    echo "  --help          Show this message"
    echo
    echo "Disable Rust acceleration after install:"
    echo "  HERMES_RI_PIPELINE=0 hermes"
    exit 0
}

# ── Parse args ──────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-rust)    SKIP_RUST=true; shift ;;
        --no-venv)      USE_VENV=false; shift ;;
        --skip-setup)   RUN_SETUP=false; shift ;;
        --branch)       BRANCH="$2"; shift 2 ;;
        --help)         show_help ;;
        *)              warn "Unknown option: $1"; show_help ;;
    esac
done

# ── OS detection ────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux)   OS="linux" ;;
        Darwin)  OS="macos" ;;
        *)       err "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
    ARCH="$(uname -m)"
    info "Detected: $OS / $ARCH"
}

# ── Check prerequisites ─────────────────────────────────────────────
check_prereqs() {
    step "Checking prerequisites..."

    if ! command -v git &>/dev/null; then
        fail "git is required. Install: apt install git / brew install git"
        exit 1
    fi
    ok "git"

    if ! command -v curl &>/dev/null; then
        fail "curl is required"
        exit 1
    fi
    ok "curl"

    if [ "$USE_VENV" = true ]; then
        if ! command -v uv &>/dev/null; then
            info "Installing uv (Rust Python package manager)..."
            curl -LsSf https://astral.sh/uv/install.sh | sh
            # shellcheck disable=SC1091
            [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
            export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
        fi
        ok "uv $(uv --version 2>/dev/null || echo 'installed')"
    fi
}

# ── Resolve install layout ──────────────────────────────────────────
resolve_layout() {
    if [ -n "$INSTALL_DIR" ]; then
        info "Using explicit install dir: $INSTALL_DIR"
        return
    fi

    if [ "$(id -u)" -eq 0 ]; then
        INSTALL_DIR="/usr/local/lib/hermes-agent"
        BIN_DIR="/usr/local/bin"
        ROOT_FHS=true
        info "Root install: code at $INSTALL_DIR, binary at $BIN_DIR"
    else
        INSTALL_DIR="$HERMES_HOME/hermes-agent"
        info "User install: $INSTALL_DIR"
    fi
}

# ── Clone repository ────────────────────────────────────────────────
clone_repo() {
    step "Cloning RecursiveIntell/hermes-agent..."

    if [ -d "$INSTALL_DIR" ]; then
        warn "$INSTALL_DIR already exists — updating..."
        git -C "$INSTALL_DIR" fetch origin "$BRANCH"
        git -C "$INSTALL_DIR" checkout "$BRANCH"
        git -C "$INSTALL_DIR" pull origin "$BRANCH" --ff-only 2>/dev/null || \
            warn "Could not fast-forward; skipping pull (local changes present)"
    else
        git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
    ok "Repository: $INSTALL_DIR"
}

# ── Set up Python environment ───────────────────────────────────────
setup_python() {
    step "Setting up Python environment..."

    if [ "$USE_VENV" = true ]; then
        cd "$INSTALL_DIR"
        uv venv --python "$PYTHON_VERSION" .venv 2>/dev/null || \
            uv venv --python "$PYTHON_VERSION"
        # shellcheck disable=SC1091
        [ -f .venv/bin/activate ] && . .venv/bin/activate
        uv pip install -e ".[all]"
        ok "uv venv + all deps"
    else
        pip3 install -e "$INSTALL_DIR[all]" 2>/dev/null || \
            pip install -e "$INSTALL_DIR[all]"
        ok "system pip install"
    fi
}

# ── Install Rust PyO3 wheels ────────────────────────────────────────
install_rust_wheels() {
    if [ "$SKIP_RUST" = true ]; then
        info "Skipping Rust wheels (--skip-rust)"
        return
    fi

    step "Installing RecursiveIntell Rust acceleration..."

    local failed=""
    for crate in llm-pipeline context-governor poly-kv; do
        if pip install "$crate" 2>/dev/null; then
            ok "$crate"
        else
            fail "$crate (will fall back to Python)"
            failed="$failed $crate"
        fi
    done

    if [ -n "$failed" ]; then
        warn "Some Rust wheels failed to install:$failed"
        warn "The agent will use pure Python fallbacks automatically."
    else
        ok "All Rust wheels installed — acceleration active by default"
    fi
}

# ── Configure RecursiveIntell defaults ───────────────────────────────
configure_ri_defaults() {
    step "Configuring RecursiveIntell defaults..."

    local config="$HERMES_HOME/config.yaml"

    # Use Rust context engine
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/.venv/bin/hermes" config set context.engine ri-context-governor 2>/dev/null || \
            warn "Could not set context engine (run 'hermes config set context.engine ri-context-governor' manually)"
    else
        hermes config set context.engine ri-context-governor 2>/dev/null || true
    fi
    ok "Context engine: ri-context-governor"
}

# ── Setup wizard ────────────────────────────────────────────────────
run_setup() {
    if [ "$RUN_SETUP" = false ]; then
        info "Skipping setup wizard (--skip-setup)"
        return
    fi

    step "Running Hermes setup..."
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/.venv/bin/hermes" setup --non-interactive 2>/dev/null || \
            warn "Setup encountered warnings (you can re-run: hermes setup)"
    else
        hermes setup --non-interactive 2>/dev/null || true
    fi
    ok "Setup complete"
}

# ── Print completion ────────────────────────────────────────────────
print_next_steps() {
    echo
    echo -e "${BOLD}${GREEN}✓ RecursiveIntell Hermes installed${NC}"
    echo
    echo -e "  ${BOLD}Start Hermes:${NC}"
    if [ "$USE_VENV" = true ]; then
        echo -e "    ${CYAN}$INSTALL_DIR/.venv/bin/hermes${NC}"
    else
        echo -e "    ${CYAN}hermes${NC}"
    fi
    echo
    echo -e "  ${BOLD}Rust acceleration:${NC} active by default"
    echo    "    llm-pipeline  → LLM transport (all OpenAI-compatible providers)"
    echo    "    context-governor → deterministic prompt compaction"
    echo    "    poly-kv       → vector scoring"
    echo
    echo -e "  ${BOLD}Disable a path:${NC}"
    echo    "    HERMES_RI_PIPELINE=0 hermes"
    echo
    echo -e "  ${BOLD}Update:${NC}"
    echo    "    cd $INSTALL_DIR && git pull && uv pip install -e '.[all]'"
    echo    "    pip install --upgrade llm-pipeline context-governor poly-kv"
    echo
    echo -e "  ${BOLD}Docs:${NC} https://github.com/RecursiveIntell/hermes-agent"
    echo
}

# ── Cleanup ─────────────────────────────────────────────────────────
cleanup() {
    if [ -n "${_RI_TMP_DIR:-}" ] && [ -d "$_RI_TMP_DIR" ]; then
        rm -rf "$_RI_TMP_DIR"
    fi
}
trap cleanup EXIT

# ── Main ────────────────────────────────────────────────────────────
main() {
    banner
    detect_os
    check_prereqs
    resolve_layout
    clone_repo
    setup_python
    install_rust_wheels
    configure_ri_defaults
    run_setup
    print_next_steps
}

main
