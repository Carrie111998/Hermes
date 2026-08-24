from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "apps/bootstrap-installer/src-tauri/src/bootstrap.rs",
    "        Some(commit.to_string())\n",
    "        Some(commit.to_ascii_lowercase())\n",
)
replace_once(
    "apps/bootstrap-installer/src-tauri/src/bootstrap.rs",
    '        let release_commit = "a".repeat(40);\n',
    '        let release_commit = "A".repeat(40);\n        let canonical_release_commit = "a".repeat(40);\n',
)
replace_once(
    "apps/bootstrap-installer/src-tauri/src/bootstrap.rs",
    '        assert_eq!(marker["pinnedCommit"], release_commit);\n',
    '        assert_eq!(marker["pinnedCommit"], canonical_release_commit);\n',
)

old_helper = '''pinned_git_free_release_commit() {
    local manifest="$INSTALL_DIR/.hermes-release.json"
    [ -f "$manifest" ] || return 1

    python3 - "$manifest" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        release = json.load(handle)
except (OSError, ValueError, TypeError):
    raise SystemExit(1)

commit = release.get("commit")
if (
    release.get("schema") != "hermes-agent-release/v1"
    or release.get("final_runtime_git_free") is not True
    or not isinstance(commit, str)
    or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
):
    raise SystemExit(1)

print(commit)
PY
}
'''
new_helper = '''pinned_git_free_release_commit() {
    local manifest="$INSTALL_DIR/.hermes-release.json"
    [ -f "$manifest" ] || return 1

    # The repository bootstrap stage runs in a fresh shell, so PYTHON_PATH from
    # prerequisites is not guaranteed to survive. Prefer Hermes' persisted
    # uv-managed Python, then fall back to an explicitly available interpreter.
    local validator_python=""
    if [ -n "${PYTHON_PATH:-}" ] && [ -x "$PYTHON_PATH" ]; then
        validator_python="$PYTHON_PATH"
    elif [ -x "$HERMES_HOME/bin/uv" ]; then
        validator_python="$("$HERMES_HOME/bin/uv" python find "$PYTHON_VERSION" 2>/dev/null || true)"
    elif command -v python3 >/dev/null 2>&1; then
        validator_python="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        validator_python="$(command -v python)"
    fi

    if [ -z "$validator_python" ] || [ ! -x "$validator_python" ]; then
        log_error "Cannot validate $manifest: no Python runtime is available."
        log_info "Run the prerequisites stage first or install Python $PYTHON_VERSION."
        return 2
    fi

    "$validator_python" - "$manifest" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        release = json.load(handle)
except (OSError, ValueError, TypeError):
    raise SystemExit(1)

commit = release.get("commit")
if (
    release.get("schema") != "hermes-agent-release/v1"
    or release.get("final_runtime_git_free") is not True
    or not isinstance(commit, str)
    or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
):
    raise SystemExit(1)

print(commit.lower())
PY
}
'''
replace_once("scripts/install.sh", old_helper, new_helper)

old_clone = '''        elif release_commit="$(pinned_git_free_release_commit)"; then
            # A reviewed release runtime is immutable by design.  Keep its
            # source, venv and local patches untouched; subsequent stages may
            # build the desktop from this exact pinned tree.
            log_info "Existing pinned git-free release runtime found; preserving commit $release_commit"
            cd "$INSTALL_DIR"
            return 0
        else
            log_error "Directory exists but is not a git repository: $INSTALL_DIR"
            log_info "Remove it or choose a different directory with --dir"
            exit 1
        fi
'''
new_clone = '''        else
            local release_status=0
            release_commit="$(pinned_git_free_release_commit)" || release_status=$?
            if [ "$release_status" -eq 0 ]; then
                # A reviewed release runtime is immutable by design. Keep its
                # source, venv and local patches untouched; subsequent stages may
                # build the desktop from this exact pinned tree.
                log_info "Existing pinned git-free release runtime found; preserving commit $release_commit"
                cd "$INSTALL_DIR"
                return 0
            elif [ "$release_status" -eq 2 ]; then
                # Do not mask an unavailable validator as a generic non-Git
                # directory error; the helper already emitted the actionable fix.
                exit 1
            else
                log_error "Directory exists but is not a git repository: $INSTALL_DIR"
                log_info "Remove it or choose a different directory with --dir"
                exit 1
            fi
        fi
'''
replace_once("scripts/install.sh", old_clone, new_clone)

old_marker = '''    local pinned_commit=""
    pinned_commit="$(pinned_git_free_release_commit)" || pinned_commit="$INSTALL_COMMIT"
    if [ -z "$pinned_commit" ]; then
'''
new_marker = '''    local pinned_commit=""
    local release_commit=""
    local release_status=0
    release_commit="$(pinned_git_free_release_commit)" || release_status=$?
    if [ "$release_status" -eq 0 ]; then
        pinned_commit="$release_commit"
        if [ -n "$INSTALL_COMMIT" ]; then
            log_warn "Ignoring installer --commit $INSTALL_COMMIT; git-free release manifest pins $pinned_commit."
        fi
    elif [ "$release_status" -eq 2 ]; then
        return 1
    else
        pinned_commit="$INSTALL_COMMIT"
    fi
    if [ -z "$pinned_commit" ]; then
'''
replace_once("scripts/install.sh", old_marker, new_marker)

p = Path("tests/test_install_git_free_release_runtime.py")
text = p.read_text(encoding="utf-8")
text = text.replace(
    "def _release_manifest(path: Path, *, valid: bool = True) -> None:\n",
    "def _release_manifest(path: Path, *, valid: bool = True, commit: str = COMMIT) -> None:\n",
    1,
)
text = text.replace('        "commit": COMMIT,\n', '        "commit": commit,\n', 1)
needle = '    assert re.fullmatch(r"[0-9a-f]{40}", marker["pinnedCommit"])\n'
replacement = '''    assert re.fullmatch(r"[0-9a-f]{40}", marker["pinnedCommit"])
    assert "Ignoring installer --commit" in result.stdout


def test_bootstrap_marker_normalizes_uppercase_release_commit(tmp_path: Path) -> None:
    install_dir = tmp_path / "hermes-agent"
    install_dir.mkdir()
    _release_manifest(install_dir, commit="A" * 40)

    result = _write_marker(install_dir)

    assert result.returncode == 0, result.stderr
    marker = json.loads((install_dir / ".hermes-bootstrap-complete").read_text(encoding="utf-8"))
    assert marker["pinnedCommit"] == COMMIT
'''
if text.count(needle) != 1:
    raise SystemExit("tests/test_install_git_free_release_runtime.py: expected marker assertion once")
p.write_text(text.replace(needle, replacement, 1), encoding="utf-8")

p = Path("tests/test_install_sh_install_method_stamp.py")
text = p.read_text(encoding="utf-8")
append = r'''


def test_git_install_stamp_is_guarded_by_git_metadata() -> None:
    text = INSTALL_SH.read_text()

    guarded = re.findall(
        r'if \[ -d "\$INSTALL_DIR/\.git" \]; then\s+'
        r'echo "git" > "\$INSTALL_DIR/\.install_method"\s+fi',
        text,
    )
    assert len(guarded) >= 2, (
        "both installer completion paths must leave .install_method absent "
        "for intentionally git-free release runtimes"
    )
'''
if "test_git_install_stamp_is_guarded_by_git_metadata" not in text:
    p.write_text(text + append, encoding="utf-8")
