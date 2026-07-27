#!/bin/bash

# Pillow 12.3 publishes Linux wheels for x86_64/aarch64 at glibc >= 2.27
# (manylinux_2_27) and musl >= 1.2. Older glibc releases such as RHEL 7 must
# build it from source. Keep this check local and deterministic rather than
# relying on a resolver/network failure to reveal the missing wheel midway
# through the main dependency install.
pillow_linux_wheel_compatible() {
    local arch="$1"
    local libc_name="$2"
    local libc_version="$3"
    local major="${libc_version%%.*}"
    local rest="${libc_version#*.}"
    local minor="${rest%%.*}"

    case "$arch" in
        x86_64|amd64|aarch64|arm64) ;;
        *) return 1 ;;
    esac
    case "$major" in ''|*[!0-9]*) return 1 ;; esac
    case "$minor" in ''|*[!0-9]*) return 1 ;; esac

    case "$libc_name" in
        glibc|GNU)
            [ "$major" -gt 2 ] || {
                [ "$major" -eq 2 ] && [ "$minor" -ge 27 ]
            }
            ;;
        musl)
            [ "$major" -gt 1 ] || {
                [ "$major" -eq 1 ] && [ "$minor" -ge 2 ]
            }
            ;;
        *) return 1 ;;
    esac
}

pillow_source_build_required() {
    [ "$OS" = "linux" ] || return 1

    local libc_info libc_name="" libc_version="" gnu_libc musl_info

    # Query the host first. platform.libc_ver() may describe the libc baseline
    # used to build the selected interpreter instead of the libc on this host.
    gnu_libc="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    case "$gnu_libc" in
        "glibc "*) libc_name="glibc"; libc_version="${gnu_libc#glibc }" ;;
    esac

    # Python before 3.14 does not reliably identify musl via platform.libc_ver,
    # so probe ldd directly before using the interpreter fallback.
    if [ -z "$libc_name" ] || [ -z "$libc_version" ]; then
        musl_info="$(ldd --version 2>&1 || true)"
        case "$musl_info" in
            *musl*)
                libc_name="musl"
                libc_version="$(
                    printf '%s\n' "$musl_info" \
                        | sed -n 's/^Version \([0-9][0-9.]*\).*$/\1/p' \
                        | head -n 1
                )"
                ;;
        esac
    fi

    # Fall back to the interpreter probe only when host tools did not identify
    # either glibc or musl.
    if [ -z "$libc_name" ] || [ -z "$libc_version" ]; then
        libc_info="$(
            "$PYTHON_PATH" - <<'PY' 2>/dev/null
import platform

name, version = platform.libc_ver()
print(f"{name}|{version}")
PY
        )" || true
        libc_name="${libc_info%%|*}"
        libc_version="${libc_info#*|}"
    fi

    if pillow_linux_wheel_compatible "$(uname -m)" "$libc_name" "$libc_version"; then
        return 1
    fi
    return 0
}

pillow_python_headers_ready() {
    "$PYTHON_PATH" - <<'PY' >/dev/null 2>&1
from pathlib import Path
import sysconfig

candidates = (
    sysconfig.get_path("include"),
    sysconfig.get_path("platinclude"),
    sysconfig.get_config_var("INCLUDEPY"),
)
seen = set()
for include_dir in candidates:
    if not include_dir or include_dir in seen:
        continue
    seen.add(include_dir)
    if (Path(include_dir) / "Python.h").is_file():
        raise SystemExit(0)
raise SystemExit(1)
PY
}

pillow_source_build_ready() {
    local compiler
    compiler="$(command -v cc 2>/dev/null || command -v gcc 2>/dev/null || true)"
    [ -n "$compiler" ] || return 1

    # The selected interpreter must expose Python.h, and the compiler must be
    # able to compile and link both required Pillow codecs. This detects the
    # actual capability instead of trusting package-manager state alone.
    pillow_python_headers_ready || return 1
    printf '%s\n' \
        '#include <zlib.h>' \
        'int main(void) { return zlibVersion() == 0; }' \
        | "$compiler" -x c - -o /dev/null -lz >/dev/null 2>&1 || return 1
    printf '%s\n' \
        '#include <stddef.h>' \
        '#include <stdio.h>' \
        '#include <jpeglib.h>' \
        'int main(void) { struct jpeg_error_mgr e; return jpeg_std_error(&e) == 0; }' \
        | "$compiler" -x c - -o /dev/null -ljpeg >/dev/null 2>&1 || return 1
}

prepare_pillow_source_build() {
    pillow_source_build_required || return 0

    log_info "Pillow 12.3 has no compatible wheel for this Linux/libc; checking source-build prerequisites..."
    if pillow_source_build_ready; then
        log_success "Pillow source-build prerequisites found"
        return 0
    fi

    local package_manager=""
    local packages=""
    local install_hint=""
    case "$DISTRO" in
        ubuntu|debian)
            package_manager="apt-get"
            packages="build-essential python3-dev libffi-dev libjpeg-dev zlib1g-dev"
            install_hint="sudo apt-get update && sudo apt-get install -y $packages"
            ;;
        fedora|rhel|centos|rocky|almalinux|ol|amzn)
            if command -v dnf >/dev/null 2>&1; then
                package_manager="dnf"
            elif command -v yum >/dev/null 2>&1; then
                package_manager="yum"
            fi
            packages="gcc python3-devel libffi-devel libjpeg-turbo-devel zlib-devel"
            install_hint="sudo ${package_manager:-dnf} install -y $packages"
            ;;
    esac

    log_warn "Pillow 12.3 must be built from source on this host, but its compiler/JPEG/zlib prerequisites are missing."
    if [ -z "$package_manager" ]; then
        log_error "Cannot provision Pillow source-build prerequisites for distro '$DISTRO'."
        log_info "Install a C compiler, Python development headers, libjpeg development headers, and zlib development headers, then re-run this installer."
        return 1
    fi

    local install_requested=false
    if [ "$(id -u)" -eq 0 ]; then
        install_requested=true
    elif command -v sudo >/dev/null 2>&1; then
        log_info "The installer itself does not require or retain root access."
        if prompt_yes_no "Install required Pillow source-build packages ($packages)? (requires sudo)" "no"; then
            install_requested=true
        fi
    fi

    if [ "$install_requested" = true ]; then
        log_info "Installing Pillow source-build prerequisites..."
        local install_succeeded=false
        case "$package_manager" in
            apt-get)
                if [ "$(id -u)" -eq 0 ]; then
                    if DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq \
                        && DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq $packages; then
                        install_succeeded=true
                    fi
                else
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq \
                        && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq $packages; then
                        install_succeeded=true
                    fi
                fi
                ;;
            dnf|yum)
                if [ "$(id -u)" -eq 0 ]; then
                    if "$package_manager" install -y $packages; then
                        install_succeeded=true
                    fi
                else
                    if sudo "$package_manager" install -y $packages; then
                        install_succeeded=true
                    fi
                fi
                ;;
        esac
        if [ "$install_succeeded" = false ]; then
            log_error "Could not install Pillow source-build prerequisites."
            log_info "Install them manually, then re-run this installer:"
            log_info "  $install_hint"
            return 1
        fi
    else
        log_error "Pillow source-build prerequisites were not installed."
        log_info "Install them manually, then re-run this installer:"
        log_info "  $install_hint"
        return 1
    fi

    if ! pillow_source_build_ready; then
        log_error "Pillow source-build prerequisites are still unavailable after package installation."
        log_info "Verify the compiler and development libraries, then re-run:"
        log_info "  $install_hint"
        return 1
    fi
    log_success "Pillow source-build prerequisites installed"
}

prepare_python_build_environment() {
    local pillow_needs_source=false
    if pillow_source_build_required; then
        pillow_needs_source=true
    fi

    # A source build needs everything in the normal Debian build-tool prompt
    # plus Pillow's codec headers. Let the Pillow provisioner own that single
    # transaction so an older host never sees two overlapping prompts.
    if [ "$pillow_needs_source" = true ]; then
        prepare_pillow_source_build
        return $?
    fi

    # On Debian/Ubuntu (including WSL), some other Python packages may need
    # build tools even when Pillow itself has a compatible wheel.
    if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
        local need_build_tools=false
        local build_tool_probe_packages="gcc python3-dev libffi-dev"
        local build_tool_install_packages="build-essential python3-dev libffi-dev"
        local pkg
        for pkg in $build_tool_probe_packages; do
            if ! dpkg -s "$pkg" &>/dev/null; then
                need_build_tools=true
                break
            fi
        done
        if [ "$need_build_tools" = true ]; then
            log_info "Some build tools may be needed for Python packages..."
            if [ "$(id -u)" -eq 0 ]; then
                if DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq \
                    && DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq $build_tool_install_packages >/dev/null 2>&1; then
                    log_success "Build tools installed"
                else
                    log_warn "Could not install build tools automatically; some Python packages may fail to build."
                fi
            elif command -v sudo &>/dev/null; then
                if sudo -n true 2>/dev/null; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq \
                        && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq $build_tool_install_packages >/dev/null 2>&1; then
                        log_success "Build tools installed"
                    else
                        log_warn "Could not install build tools automatically; some Python packages may fail to build."
                    fi
                else
                    log_info "sudo is needed ONLY to install build tools ($build_tool_install_packages) via apt."
                    log_info "Hermes Agent itself does not require or retain root access."
                    if prompt_yes_no "Install build tools?" "yes"; then
                        if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq \
                            && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq $build_tool_install_packages >/dev/null 2>&1; then
                            log_success "Build tools installed"
                        else
                            log_warn "Could not install build tools automatically; some Python packages may fail to build."
                        fi
                    fi
                fi
            fi
        fi
    fi
}
