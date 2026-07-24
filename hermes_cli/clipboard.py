"""Clipboard image extraction for macOS, Windows, Linux, and WSL2.

Provides a single function `save_clipboard_image(dest)` that checks the
system clipboard for image data, saves it to *dest* as PNG, and returns
True on success.  No external Python dependencies — uses only OS-level
CLI tools that ship with the platform (or are commonly installed).

Platform support:
  macOS   — osascript (always available), pngpaste (if installed)
  Windows — PowerShell via WinForms, Get-Clipboard, file-drop fallback
  WSL2    — powershell.exe via WinForms, Get-Clipboard, file-drop fallback
  Linux   — wl-paste (Wayland), xclip (X11)
"""

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

from hermes_constants import is_wsl as _is_wsl

logger = logging.getLogger(__name__)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def save_clipboard_image(dest: Path) -> bool:
    """Extract an image from the system clipboard and save it as PNG.

    Returns True if an image was found and saved, False otherwise.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        return _macos_save(dest)
    if sys.platform == "win32":
        return _windows_save(dest)
    return _linux_save(dest)


def has_clipboard_image() -> bool:
    """Quick check: does the clipboard currently contain an image?

    Lighter than save_clipboard_image — doesn't extract or write anything.
    """
    if sys.platform == "darwin":
        return _macos_has_image()
    if sys.platform == "win32":
        return _windows_has_image()
    # Match _linux_save fallthrough order: WSL → Wayland → X11
    if _is_wsl() and _wsl_has_image():
        return True
    if os.environ.get("WAYLAND_DISPLAY") and _wayland_has_image():
        return True
    return _xclip_has_image()


def read_clipboard_text() -> str:
    """Read text content from the system clipboard.

    Returns the text if any is available, or an empty string if the clipboard
    holds no text (or only images / file drops / etc.). Tries the same
    backends as ``save_clipboard_image``: WSL → Wayland → X11 → macOS →
    native Windows. Never raises — failed backends return ''.

    Added so ``tui_gateway`` ``clipboard.paste`` can fall back to text when
    the clipboard holds no image (which used to surface as a confusing
    "No image found in clipboard" message every time the user pressed
    Ctrl+Shift+V with text in the clipboard).
    """
    if sys.platform == "darwin":
        return _macos_read_text()
    if sys.platform == "win32":
        return _windows_read_text()
    # Linux — match _linux_save fallthrough order: WSL → Wayland → X11
    if _is_wsl():
        text = _wsl_read_text()
        if text:
            return text
    if os.environ.get("WAYLAND_DISPLAY"):
        text = _wayland_read_text()
        if text:
            return text
    return _xclip_read_text()


# ── macOS ────────────────────────────────────────────────────────────────

def _macos_save(dest: Path) -> bool:
    """Try pngpaste first (fast, handles more formats), fall back to osascript."""
    return _macos_pngpaste(dest) or _macos_osascript(dest)


def _macos_has_image() -> bool:
    """Check if macOS clipboard contains image data."""
    try:
        info = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return "«class PNGf»" in info.stdout or "«class TIFF»" in info.stdout
    except Exception:
        return False


def _macos_pngpaste(dest: Path) -> bool:
    """Use pngpaste (brew install pngpaste) — fastest, cleanest."""
    try:
        r = subprocess.run(
            ["pngpaste", str(dest)],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass  # pngpaste not installed
    except Exception as e:
        logger.debug("pngpaste failed: %s", e)
    return False


def _macos_osascript(dest: Path) -> bool:
    """Use osascript to extract PNG data from clipboard (always available)."""
    if not _macos_has_image():
        return False

    # Extract as PNG
    script = (
        'try\n'
        '  set imgData to the clipboard as «class PNGf»\n'
        f'  set f to open for access POSIX file "{dest}" with write permission\n'
        '  write imgData to f\n'
        '  close access f\n'
        'on error\n'
        '  return "fail"\n'
        'end try\n'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if r.returncode == 0 and "fail" not in r.stdout and dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as e:
        logger.debug("osascript clipboard extract failed: %s", e)
    return False


def _macos_read_text() -> str:
    """Read text from the macOS clipboard via pbpaste."""
    try:
        r = subprocess.run(
            ["pbpaste"],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        logger.debug("pbpaste not found — macOS clipboard text unavailable")
    except Exception as e:
        logger.debug("pbpaste failed: %s", e)
    return ""


# ── Shared PowerShell scripts (native Windows + WSL2) ─────────────────────

# .NET System.Windows.Forms.Clipboard — used by both native Windows (powershell)
# and WSL2 (powershell.exe) paths.
_PS_CHECK_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[System.Windows.Forms.Clipboard]::ContainsImage()"
)

_PS_EXTRACT_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$img = [System.Windows.Forms.Clipboard]::GetImage();"
    "if ($null -eq $img) { exit 1 }"
    "$ms = New-Object System.IO.MemoryStream;"
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);"
    "[System.Convert]::ToBase64String($ms.ToArray())"
)

_PS_CHECK_IMAGE_GET_CLIPBOARD = (
    "try { "
    "$img = Get-Clipboard -Format Image -ErrorAction Stop;"
    "if ($null -ne $img) { 'True' } else { 'False' }"
    "} catch { 'False' }"
)

_PS_EXTRACT_IMAGE_GET_CLIPBOARD = (
    "try { "
    "Add-Type -AssemblyName System.Drawing;"
    "Add-Type -AssemblyName PresentationCore;"
    "Add-Type -AssemblyName WindowsBase;"
    "$img = Get-Clipboard -Format Image -ErrorAction Stop;"
    "if ($null -eq $img) { exit 1 }"
    "$ms = New-Object System.IO.MemoryStream;"
    "if ($img -is [System.Drawing.Image]) {"
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)"
    "} elseif ($img -is [System.Windows.Media.Imaging.BitmapSource]) {"
    "$enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder;"
    "$enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($img));"
    "$enc.Save($ms)"
    "} else { exit 2 }"
    "[System.Convert]::ToBase64String($ms.ToArray())"
    "} catch { exit 1 }"
)

_FILEDROP_IMAGE_EXTS = "'.png','.jpg','.jpeg','.gif','.webp','.bmp','.tiff','.tif'"

_PS_CHECK_FILEDROP_IMAGE = (
    "try { "
    "$files = Get-Clipboard -Format FileDropList -ErrorAction Stop;"
    f"$exts = @({_FILEDROP_IMAGE_EXTS});"
    "$hit = $files | Where-Object { $exts -contains ([System.IO.Path]::GetExtension($_).ToLowerInvariant()) } | Select-Object -First 1;"
    "if ($null -ne $hit) { 'True' } else { 'False' }"
    "} catch { 'False' }"
)

_PS_EXTRACT_FILEDROP_IMAGE = (
    "try { "
    "$files = Get-Clipboard -Format FileDropList -ErrorAction Stop;"
    f"$exts = @({_FILEDROP_IMAGE_EXTS});"
    "$hit = $files | Where-Object { $exts -contains ([System.IO.Path]::GetExtension($_).ToLowerInvariant()) } | Select-Object -First 1;"
    "if ($null -eq $hit) { exit 1 }"
    "[System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($hit))"
    "} catch { exit 1 }"
)

_POWERSHELL_HAS_IMAGE_SCRIPTS = (
    _PS_CHECK_IMAGE,
    _PS_CHECK_IMAGE_GET_CLIPBOARD,
    _PS_CHECK_FILEDROP_IMAGE,
)

_POWERSHELL_EXTRACT_IMAGE_SCRIPTS = (
    _PS_EXTRACT_IMAGE,
    _PS_EXTRACT_IMAGE_GET_CLIPBOARD,
    _PS_EXTRACT_FILEDROP_IMAGE,
)


# Text extraction — uses Get-Clipboard (PowerShell 5.1+) which returns text
# directly. Works on both native Windows (powershell/pwsh) and WSL2
# (powershell.exe → Windows clipboard).
_PS_READ_TEXT = (
    "try { "
    "$txt = Get-Clipboard -Raw -ErrorAction Stop; "
    "if ($null -eq $txt) { '' } else { [Console]::Out.Write($txt) } "
    "} catch { '' }"
)


def _run_powershell(exe: str, script: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
    )


def _write_base64_image(dest: Path, b64_data: str) -> bool:
    image_bytes = base64.b64decode(b64_data, validate=True)
    dest.write_bytes(image_bytes)
    return dest.exists() and dest.stat().st_size > 0


def _powershell_has_image(exe: str, *, timeout: int, label: str) -> bool:
    for script in _POWERSHELL_HAS_IMAGE_SCRIPTS:
        try:
            r = _run_powershell(exe, script, timeout=timeout)
            if r.returncode == 0 and "True" in r.stdout:
                return True
        except FileNotFoundError:
            logger.debug("%s not found — clipboard unavailable", exe)
            return False
        except Exception as e:
            logger.debug("%s clipboard image check failed: %s", label, e)
    return False


def _powershell_save_image(exe: str, dest: Path, *, timeout: int, label: str) -> bool:
    for script in _POWERSHELL_EXTRACT_IMAGE_SCRIPTS:
        try:
            r = _run_powershell(exe, script, timeout=timeout)
            if r.returncode != 0:
                continue

            b64_data = r.stdout.strip()
            if not b64_data:
                continue

            if _write_base64_image(dest, b64_data):
                return True
        except FileNotFoundError:
            logger.debug("%s not found — clipboard unavailable", exe)
            return False
        except Exception as e:
            logger.debug("%s clipboard image extraction failed: %s", label, e)
            dest.unlink(missing_ok=True)
    return False


# ── Native Windows ────────────────────────────────────────────────────────

# Native Windows uses ``powershell`` (Windows PowerShell 5.1, always present)
# or ``pwsh`` (PowerShell 7+, optional).  Discovery is cached per-process.


def _find_powershell() -> str | None:
    """Return the first available PowerShell executable, or None."""
    for name in ("powershell", "pwsh"):
        try:
            r = subprocess.run(
                [name, "-NoProfile", "-NonInteractive", "-Command", "echo ok"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
            )
            if r.returncode == 0 and "ok" in r.stdout:
                return name
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return None


# Cache the resolved PowerShell executable (checked once per process)
_ps_exe: str | None | bool = False  # False = not yet checked


def _get_ps_exe() -> str | None:
    global _ps_exe
    if _ps_exe is False:
        _ps_exe = _find_powershell()
    return _ps_exe


def _windows_has_image() -> bool:
    """Check if the Windows clipboard contains an image."""
    ps = _get_ps_exe()
    if ps is None:
        return False
    return _powershell_has_image(ps, timeout=5, label="Windows")


def _windows_save(dest: Path) -> bool:
    """Extract clipboard image on native Windows via PowerShell → base64 PNG."""
    ps = _get_ps_exe()
    if ps is None:
        logger.debug("No PowerShell found — Windows clipboard image paste unavailable")
        return False
    return _powershell_save_image(ps, dest, timeout=15, label="Windows")


def _windows_read_text() -> str:
    """Read text from the Windows clipboard via PowerShell `Get-Clipboard -Raw`."""
    ps = _get_ps_exe()
    if ps is None:
        return ""
    try:
        r = _run_powershell(ps, _PS_READ_TEXT, timeout=5)
        if r.returncode == 0:
            # PowerShell adds a trailing newline to its stdout pipeline output,
            # even when Get-Clipboard returned an empty string. Strip the single
            # trailing newline so an empty clipboard reads as '' not '\n'.
            return r.stdout.rstrip("\n").rstrip("\r")
    except Exception as e:
        logger.debug("Windows clipboard text read failed: %s", e)
    return ""


# ── Linux ────────────────────────────────────────────────────────────────

def _linux_save(dest: Path) -> bool:
    """Try clipboard backends in priority order: WSL → Wayland → X11."""
    if _is_wsl():
        if _wsl_save(dest):
            return True
        # Fall through — WSLg might have wl-paste or xclip working

    if os.environ.get("WAYLAND_DISPLAY"):
        if _wayland_save(dest):
            return True

    return _xclip_save(dest)


# ── WSL2 (powershell.exe) ────────────────────────────────────────────────
# Reuses _PS_CHECK_IMAGE / _PS_EXTRACT_IMAGE defined above.

def _wsl_has_image() -> bool:
    """Check if Windows clipboard has an image (via powershell.exe)."""
    return _powershell_has_image("powershell.exe", timeout=8, label="WSL")


def _wsl_save(dest: Path) -> bool:
    """Extract clipboard image via powershell.exe → base64 → decode to PNG."""
    return _powershell_save_image("powershell.exe", dest, timeout=15, label="WSL")


def _wsl_read_text() -> str:
    """Read text from the Windows clipboard via powershell.exe (WSL2 path)."""
    try:
        r = _run_powershell("powershell.exe", _PS_READ_TEXT, timeout=8)
        if r.returncode == 0:
            # Same trailing-newline quirk as native Windows — see
            # _windows_read_text() for the rationale.
            return r.stdout.rstrip("\n").rstrip("\r")
    except FileNotFoundError:
        logger.debug("powershell.exe not found — WSL clipboard text unavailable")
    except Exception as e:
        logger.debug("WSL clipboard text read failed: %s", e)
    return ""


# ── Wayland (wl-paste) ──────────────────────────────────────────────────

def _wayland_has_image() -> bool:
    """Check if Wayland clipboard has image content."""
    try:
        r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return r.returncode == 0 and any(
            t.startswith("image/") for t in r.stdout.splitlines()
        )
    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard unavailable")
    except Exception:
        pass
    return False


def _wayland_save(dest: Path) -> bool:
    """Use wl-paste to extract clipboard image (Wayland sessions)."""
    try:
        # Check available MIME types
        types_r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        if types_r.returncode != 0:
            return False
        types = types_r.stdout.splitlines()

        # Prefer PNG, fall back to other image formats
        mime = None
        for preferred in ("image/png", "image/jpeg", "image/bmp",
                          "image/gif", "image/webp"):
            if preferred in types:
                mime = preferred
                break

        if not mime:
            return False

        # Extract the image data
        with open(dest, "wb") as f:
            subprocess.run(
                ["wl-paste", "--type", mime],
                stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True,
            )

        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            return False

        # save_clipboard_image() promises a PNG output path. Wayland can offer
        # JPEG/GIF/WebP/BMP payloads, so normalize every non-PNG result before
        # returning success.
        if mime != "image/png":
            if not _convert_to_png(dest) or not _is_png_file(dest):
                dest.unlink(missing_ok=True)
                return False

        return True

    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard unavailable")
    except Exception as e:
        logger.debug("wl-paste clipboard extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False


def _wayland_read_text() -> str:
    """Read text from the Wayland clipboard via `wl-paste --no-newline`."""
    try:
        # wl-paste adds a trailing newline by default; --no-newline preserves
        # the original text exactly as it was copied. We still rstrip() to be
        # safe against misbehaving clipboard managers.
        r = subprocess.run(
            ["wl-paste", "--no-newline"],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0:
            text = r.stdout.decode("utf-8", errors="replace")
            return text.rstrip("\n")
    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard text unavailable")
    except Exception as e:
        logger.debug("wl-paste text read failed: %s", e)
    return ""


def _convert_to_png(path: Path) -> bool:
    """Convert an image file to PNG in-place (requires Pillow or ImageMagick)."""
    # Try Pillow first (likely installed in the venv)
    try:
        from PIL import Image
        img = Image.open(path)
        img.save(path, "PNG")
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("Pillow BMP→PNG conversion failed: %s", e)

    # Fall back to ImageMagick convert
    tmp = path.with_suffix(".bmp")
    try:
        path.rename(tmp)
        r = subprocess.run(
            ["convert", str(tmp), "png:" + str(path)],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0 and path.exists() and path.stat().st_size > 0:
            tmp.unlink(missing_ok=True)
            return True
        else:
            # Convert failed — restore the original file
            tmp.rename(path)
    except FileNotFoundError:
        logger.debug("ImageMagick not installed — cannot convert BMP to PNG")
        if tmp.exists() and not path.exists():
            tmp.rename(path)
    except Exception as e:
        logger.debug("ImageMagick BMP→PNG conversion failed: %s", e)
        if tmp.exists() and not path.exists():
            tmp.rename(path)

    # Can't convert — BMP is still usable as-is for most APIs
    return path.exists() and path.stat().st_size > 0


def _is_png_file(path: Path) -> bool:
    """Return True when *path* starts with the PNG file signature."""
    try:
        with path.open("rb") as f:
            return f.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
    except OSError:
        return False


# ── X11 (xclip) ─────────────────────────────────────────────────────────

def _xclip_has_image() -> bool:
    """Check if X11 clipboard has image content."""
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return r.returncode == 0 and "image/png" in r.stdout
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return False


def _xclip_save(dest: Path) -> bool:
    """Use xclip to extract clipboard image (X11 sessions)."""
    # Check if clipboard has image content
    try:
        targets = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        if "image/png" not in targets.stdout:
            return False
    except FileNotFoundError:
        logger.debug("xclip not installed — X11 clipboard image paste unavailable")
        return False
    except Exception:
        return False

    # Extract PNG data
    try:
        with open(dest, "wb") as f:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True,
            )
        if dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as e:
        logger.debug("xclip image extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False


def _xclip_read_text() -> str:
    """Read text from the X11 clipboard via `xclip -selection clipboard -o`.

    Reads the CLIPBOARD selection (Ctrl+C / Ctrl+V) rather than PRIMARY
    (mouse middle-click). Returns '' if xclip is missing, the clipboard has
    no text (e.g. only an image), or any backend error occurs.
    """
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        logger.debug("xclip not installed — X11 clipboard text unavailable")
    except Exception as e:
        logger.debug("xclip text read failed: %s", e)
    return ""
