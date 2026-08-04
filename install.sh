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
WITH_SEMANTIC_MEMORY=false
WITH_AGENT_GRAPH=false
WITH_JOSH_SETUP=false

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
    echo "  --skip-rust              Skip PyO3 wheel installation"
    echo "  --no-venv                Use system Python instead of uv venv"
    echo "  --skip-setup             Skip post-install setup wizard"
    echo "  --branch NAME            Install from a specific branch (default: main)"
    echo
    echo "  --with-semantic-memory   Install semantic-memory MCP server (knowledge base)"
    echo "  --with-agent-graph       Install agent-graph MCP server (multi-agent graphs)"
    echo "  --with-all-mcp           Install both MCP servers above"
    echo "  --with-josh-setup        Full Josh's setup: skills + hooks + MCP servers"
    echo "  --help                   Show this message"
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
        --with-semantic-memory)  WITH_SEMANTIC_MEMORY=true; shift ;;
        --with-agent-graph)      WITH_AGENT_GRAPH=true; shift ;;
        --with-all-mcp) WITH_SEMANTIC_MEMORY=true; WITH_AGENT_GRAPH=true; shift ;;
        --with-josh-setup) WITH_JOSH_SETUP=true; shift ;;
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

# ── Install MCP servers (prebuilt binaries) ──────────────────────────
install_mcp_servers() {
    local installed_any=false

    # ── semantic-memory ──────────────────────────────────────────
    if [ "$WITH_SEMANTIC_MEMORY" = true ]; then
        echo
        step "Installing semantic-memory MCP server..."
        info "  Knowledge base + memory store. Lets Hermes remember across sessions,"
        info "  search past conversations, and build a personal knowledge graph."

        local asset="semantic-memory-mcp-linux-x64"
        local url="https://github.com/RecursiveIntell/semantic-memory-mcp/releases/latest/download/${asset}"
        local dest="$HOME/.local/bin/semantic-memory-mcp"

        if [ "$OS" != "linux" ]; then
            warn "  Prebuilt binary only available for Linux. Build from source:"
            warn "    cargo install semantic-memory-mcp"
        elif curl -fsSL "$url" -o "$dest" 2>/dev/null; then
            chmod +x "$dest"
            ok "semantic-memory-mcp → $dest"
            installed_any=true
        else
            fail "semantic-memory-mcp download failed. Build from source:"
            fail "  cargo install semantic-memory-mcp"
        fi
    fi

    # ── agent-graph ──────────────────────────────────────────────
    if [ "$WITH_AGENT_GRAPH" = true ]; then
        echo
        step "Installing agent-graph MCP server..."
        info "  Multi-agent graph orchestration. Run 9+ LLM nodes in parallel fan-out,"
        info "  council deliberation, plan→critique→refine pipelines, HITL approvals."

        local asset="agent-graph-mcp-linux-x64"
        local url="https://github.com/RecursiveIntell/agent-graph-mcp/releases/latest/download/${asset}"
        local proxy_dest="$HOME/.local/bin/agent-graph-mcp"
        local daemon_dest="$HOME/.local/bin/agent-graph-mcpd"

        if [ "$OS" != "linux" ]; then
            warn "  Prebuilt binary only available for Linux. Build from source:"
            warn "    cargo install agent-graph-mcp"
        elif curl -fsSL "$url" -o "$proxy_dest" 2>/dev/null; then
            chmod +x "$proxy_dest"
            ok "agent-graph-mcp → $proxy_dest"
            # The release asset is a combined binary; symlink for daemon
            ln -sf "$proxy_dest" "$daemon_dest"
            ok "agent-graph-mcpd → $daemon_dest (symlink)"
            installed_any=true
        else
            fail "agent-graph-mcp download failed. Build from source:"
            fail "  cargo install agent-graph-mcp"
        fi
    fi

    # ── Coming soon ──────────────────────────────────────────────
    if [ "$WITH_SEMANTIC_MEMORY" = true ] || [ "$WITH_AGENT_GRAPH" = true ]; then
        echo
        info "Coming soon (source-only for now):"
        info "  claim-ledger-mcp    — evidence/claim verification ledger"
        info "  forge-memory-bridge — Forge → semantic-memory import bridge"
        info "  Build with: cargo install <crate-name>"
    fi

    if [ "$installed_any" = true ]; then
        echo
        ok "MCP servers installed. Registering in Hermes config..."

        # Register semantic-memory if installed
        if [ "$WITH_SEMANTIC_MEMORY" = true ]; then
            if [ "$USE_VENV" = true ]; then
                "$INSTALL_DIR/.venv/bin/hermes" config set mcp_servers.semantic_memory.command "$HOME/.local/bin/semantic-memory-mcp" 2>/dev/null
                "$INSTALL_DIR/.venv/bin/hermes" config set mcp_servers.semantic_memory.enabled true 2>/dev/null
            else
                hermes config set mcp_servers.semantic_memory.command "$HOME/.local/bin/semantic-memory-mcp" 2>/dev/null
                hermes config set mcp_servers.semantic_memory.enabled true 2>/dev/null
            fi
            ok "semantic-memory registered"
        fi

        # Register agent-graph if installed
        if [ "$WITH_AGENT_GRAPH" = true ]; then
            if [ "$USE_VENV" = true ]; then
                "$INSTALL_DIR/.venv/bin/hermes" config set mcp_servers.agent_graph.command "$HOME/.local/bin/agent-graph-mcp" 2>/dev/null
                "$INSTALL_DIR/.venv/bin/hermes" config set mcp_servers.agent_graph.enabled true 2>/dev/null
            else
                hermes config set mcp_servers.agent_graph.command "$HOME/.local/bin/agent-graph-mcp" 2>/dev/null
                hermes config set mcp_servers.agent_graph.enabled true 2>/dev/null
            fi
            ok "agent-graph registered"
        fi

        echo
        info "MCP servers will be available after restarting Hermes."
        info "Agent hooks auto-discover from ~/.hermes/agent-hooks/ — no config needed."
    fi
}

# ── Install Josh's full setup ────────────────────────────────────────
install_josh_setup() {
    if [ "$WITH_JOSH_SETUP" != true ]; then
        return
    fi

    echo
    echo -e "${BOLD}${CYAN}  ═══════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${CYAN}  ▸ Josh's Hermes Setup${NC}"
    echo -e "${BOLD}${CYAN}  ═══════════════════════════════════════════════${NC}"
    echo

    # Enable MCP servers if not already selected
    if [ "$WITH_SEMANTIC_MEMORY" != true ] || [ "$WITH_AGENT_GRAPH" != true ]; then
        info "Enabling MCP servers (required for full setup)..."
        WITH_SEMANTIC_MEMORY=true
        WITH_AGENT_GRAPH=true
        install_mcp_servers
    fi

    local release_url="https://github.com/RecursiveIntell/hermes-agent/releases/latest/download"

    # ── Skills (70+ skills) ──────────────────────────────────────
    echo
    step "Installing Josh's skills pack (70+ skills)..."
    info "  README generation, code review, GPU benchmarking, council deliberation,"
    info "  device maintenance, email automation, GPU kernel dev, and more."

    local skills_tarball="/tmp/hermes-skills.tar.gz"
    if curl -fsSL "${release_url}/hermes-skills-20260803.tar.gz" -o "$skills_tarball" 2>/dev/null; then
        mkdir -p "$HERMES_HOME/skills"
        tar -xzf "$skills_tarball" -C "$HERMES_HOME/skills" 2>/dev/null && \
            ok "Skills installed → $HERMES_HOME/skills/" && \
            rm -f "$skills_tarball" || {
            fail "Failed to extract skills tarball"
            rm -f "$skills_tarball"
        }
    else
        fail "Skills download failed — skipping (Hermes will still work)"
    fi

    # ── Agent hooks (12 hooks) ───────────────────────────────────
    echo
    step "Installing Josh's agent hooks (12 hooks)..."
    info "  context-governor compaction, semantic-memory recall/capture,"
    info "  CEA edit telemetry, knowledge-router classification, council trimming."

    local hooks_tarball="/tmp/hermes-hooks.tar.gz"
    if curl -fsSL "${release_url}/hermes-hooks-20260803.tar.gz" -o "$hooks_tarball" 2>/dev/null; then
        mkdir -p "$HERMES_HOME/agent-hooks"
        tar -xzf "$hooks_tarball" -C "$HERMES_HOME/agent-hooks" 2>/dev/null && \
            ok "Hooks installed → $HERMES_HOME/agent-hooks/" && \
            rm -f "$hooks_tarball" || {
            fail "Failed to extract hooks tarball"
            rm -f "$hooks_tarball"
        }
    else
        fail "Hooks download failed — skipping (Hermes will still work)"
    fi

    echo
    echo -e "${BOLD}${GREEN}  ✓ Josh's setup complete${NC}"
    echo
    echo -e "  ${BOLD}What you get:${NC}"
    echo    "    • 70+ skills — READMEs, code review, GPU benchmarking, email, etc."
    echo    "    • 12 agent hooks — memory recall, context compaction, telemetry"
    echo    "    • semantic-memory MCP server — knowledge base + search (auto-registered)"
    echo    "    • agent-graph MCP server — multi-agent graphs (auto-registered)"
    echo    "    • Built-in memory disabled — semantic-memory replaces it"
    echo
    echo -e "  ${BOLD}What you still need to provide:${NC}"
    echo    "    • LLM provider API key (set OPENAI_API_KEY)"
    echo    "    • Start agent-graph daemon: agent-graph-mcpd --base-url <url> --model <model> &"
    echo    "    • Start semantic-memory daemon if using persistent mode"
    echo    "    • Run 'hermes setup' to configure your LLM providers"
    echo
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

    # If semantic-memory was installed, disable built-in memory and configure MCP
    if [ "$WITH_SEMANTIC_MEMORY" = true ] || [ "$WITH_AGENT_GRAPH" = true ]; then
        step "Configuring MCP servers..."
        local py
        if [ "$USE_VENV" = true ] && [ -f "$INSTALL_DIR/.venv/bin/python" ]; then
            py="$INSTALL_DIR/.venv/bin/python"
        else
            py="python3"
        fi
        "$py" -c "
import yaml, os
cfg_path = os.path.expanduser('$HERMES_HOME/config.yaml')
with open(cfg_path) as f:
    cfg = yaml.safe_load(f) or {}

# Disable built-in memory if semantic-memory is installed
if '${WITH_SEMANTIC_MEMORY}' == 'true':
    cfg.setdefault('agent', {})
    disabled = cfg['agent'].setdefault('disabled_toolsets', [])
    if 'memory' not in disabled:
        disabled.append('memory')

# Set agent-graph socket args if installed
if '${WITH_AGENT_GRAPH}' == 'true':
    cfg.setdefault('mcp_servers', {})
    ag = cfg['mcp_servers'].setdefault('agent_graph', {})
    ag.setdefault('args', ['--socket', '/tmp/agent-graph/mcp.sock'])

with open(cfg_path, 'w') as f:
    yaml.safe_dump(cfg, f, default_flow_style=False)
" 2>/dev/null && ok "MCP config written" || warn "Could not write MCP config — configure mcp_servers manually"
    fi
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

    if [ "$WITH_SEMANTIC_MEMORY" = true ] || [ "$WITH_AGENT_GRAPH" = true ]; then
        echo -e "  ${BOLD}MCP servers installed + registered:${NC}"
        [ "$WITH_SEMANTIC_MEMORY" = true ] && echo "    semantic-memory → knowledge base + memory search (built-in memory disabled)"
        [ "$WITH_AGENT_GRAPH" = true ] && echo "    agent-graph     → multi-agent graph orchestration (daemon: agent-graph-mcpd)"
        echo
    fi

    if [ "$WITH_JOSH_SETUP" = true ]; then
        echo -e "  ${BOLD}Josh's setup:${NC} skills + hooks installed"
        echo    "    Skills: $HERMES_HOME/skills/"
        echo    "    Hooks:  $HERMES_HOME/agent-hooks/"
        echo    "    Start agent-graph daemon: agent-graph-mcpd --model <your-model> &"
        echo
    fi

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
    install_mcp_servers
    install_josh_setup
    configure_ri_defaults
    run_setup
    print_next_steps
}

main
