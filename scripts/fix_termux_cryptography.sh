#!/usr/bin/env bash
# Fix the Termux/Android cryptography 50.0.0 runtime symbol-resolution bug.
#
# Root cause (NousResearch/hermes-agent#83680): cryptography>=50.0.0's on-device
# Android wheel builds `_rust.abi3.so` WITHOUT libpython in its DT_NEEDED list
# (confirmed via readelf: DT_NEEDED = libssl/libcrypto/libdl/libc only). On
# Termux, Python is built with Py_ENABLE_SHARED=1, so the dynamic loader cannot
# resolve Python C-API symbols (PyLong_Type, PyBaseObject_Type, ...) when the
# Rust extension is dlopened, and Hermes fails to register its bundled secret
# sources ("hermes update"/"hermes doctor --fix" exit 1).
#
# Fix: add libpython to the .so's DT_NEEDED with patchelf (the offline stopgap
# confirmed working in the issue thread). Idempotent: skips if libpython is
# already present. Runs after `pip install` on Termux only.
#
# Note: this is a stopgap. A future `pip install --reinstall cryptography` (from
# source) will rebuild the wheel and re-break it, so re-run this script after any
# cryptography reinstall. The durable fix is to use the distro package
# (`pkg install python-cryptography`), which links libpython — tracked for a
# later change.

set -euo pipefail

log_info() { printf '\033[0;34m[fix-crypto]\033[0m %s\n' "$1"; }
log_warn() { printf '\033[0;33m[fix-crypto]\033[0m %s\n' "$1"; }
log_ok()   { printf '\033[0;32m[fix-crypto]\033[0m %s\n' "$1"; }

# Only relevant on Termux / Android.
if [ -z "${TERMUX_VERSION:-}" ] && [[ "${PREFIX:-}" != *"com.termux/files/usr"* ]]; then
  log_info "Not Termux — nothing to do."
  exit 0
fi

PYTHON_BIN="${1:-}"
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  log_warn "No python3 found — skipping cryptography DT_NEEDED fix."
  exit 0
fi

# Locate the cryptography _rust extension in the venv/site-packages.
RUST_SO="$("$PYTHON_BIN" - "$PYTHON_BIN" <<'PY'
import sys, glob, os
try:
    import cryptography
except Exception:
    sys.exit(0)
base = os.path.dirname(cryptography.__file__)
hits = glob.glob(os.path.join(base, "hazmat", "bindings", "_rust*.so"))
print(hits[0] if hits else "", end="")
PY
)"
if [ -z "$RUST_SO" ] || [ ! -f "$RUST_SO" ]; then
  log_info "cryptography not importable / no _rust*.so found — nothing to patch."
  exit 0
fi

# Determine the libpython shared object Termux ships.
LIBPY="$(ls "$PREFIX"/lib/libpython*.so 2>/dev/null | head -n1 || true)"
if [ -z "$LIBPY" ]; then
  log_warn "No libpython*.so in \$PREFIX/lib ($PREFIX) — cannot patch DT_NEEDED."
  exit 0
fi
LIBPY_BASENAME="$(basename "$LIBPY")"

# Check whether libpython is already in DT_NEEDED.
if readelf -d "$RUST_SO" 2>/dev/null | grep -q "$LIBPY_BASENAME"; then
  log_ok "$RUST_SO already NEEDEDs $LIBPY_BASENAME — skipping."
  exit 0
fi

# Ensure patchelf is available.
if ! command -v patchelf >/dev/null 2>&1; then
  log_info "Installing patchelf (Termux)…"
  if command -v pkg >/dev/null 2>&1; then
    pkg install -y patchelf >/dev/null 2>&1 || log_warn "pkg install patchelf failed — install it manually."
  fi
fi
if ! command -v patchelf >/dev/null 2>&1; then
  log_warn "patchelf unavailable — cannot patch $RUST_SO."
  log_info "Manual fix: patchelf --add-needed $LIBPY_BASENAME $RUST_SO"
  exit 0
fi

log_info "Patching $RUST_SO to NEEDED $LIBPY_BASENAME"
if patchelf --add-needed "$LIBPY_BASENAME" "$RUST_SO" 2>/dev/null; then
  log_ok "Patched. Re-run 'hermes doctor' to confirm."
else
  log_warn "patchelf failed (permissions?). Try: patchelf --add-needed $LIBPY_BASENAME $RUST_SO"
  exit 0
fi
