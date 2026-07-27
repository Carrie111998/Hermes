"""Native Windows backend for computer_use.

Delegates ALL physical operations to GA's ljqCtrl (battle-tested, proven DPI
handling, click verification, robust window activation). No re-invention.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import GA's ljqCtrl (proven physical ops)
# ---------------------------------------------------------------------------
_ljqCtrl = None

def _ensure_ljq():
    global _ljqCtrl
    if _ljqCtrl is not None:
        return
    # Import bundled ljqCtrl (self-contained, no external paths)
    from tools.computer_use import _ljqctrl as _l
    _ljqCtrl = _l
    logger.info("win32_backend: using bundled _ljqctrl (dpi_scale=%.4f)", _ljqCtrl.dpi_scale)

class Win32NativeBackend(ComputerUseBackend):
    """Hermes computer_use backend wrapping GA's battle-tested ljqCtrl."""

    def __init__(self):
        self._started = False

    def start(self) -> None:
        _ensure_ljq()
        self._started = True

    def stop(self) -> None:
        self._started = False

    def is_available(self) -> bool:
        try:
            _ensure_ljq()
            return True
        except Exception:
            return False

    # -- screenshot ----------------------------------------------------------

    def capture(self, mode="som", app=None, pid=None, window_id=None):
        _ensure_ljq()
        hwnd = None
        img = None
        if pid is not None:
            hwnd = self._find_hwnd(pid=pid)
        elif app or window_id:
            hwnd = self._find_hwnd(app=app or window_id)
        if hwnd:
            try:
                img = _ljqCtrl.GrabWindow(hwnd)
            except Exception:
                img = None
        if img is None:
            from PIL import ImageGrab
            img = ImageGrab.grab()
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return CaptureResult(
            mode=mode, width=w, height=h,
            png_b64=base64.b64encode(buf.getvalue()).decode("ascii"),
            elements=[], png_bytes_len=buf.tell(),
        )

    def _find_hwnd(self, app=None, pid=None):
        """Find window handle by title (substring) or pid."""
        import win32gui, ctypes
        if pid is not None:
            candidates = []
            def _enum(hwnd, lst):
                if isinstance(lst, list):
                    p = ctypes.c_uint()
                    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(p))
                    if p.value == pid:
                        lst.append(hwnd)
            win32gui.EnumWindows(_enum, candidates)
            return candidates[0] if candidates else None
        if app:
            hwnd = win32gui.FindWindow(None, app)
            if hwnd:
                return hwnd
            matches = []
            def _enum_title(hwnd, lst):
                if isinstance(lst, list):
                    t = win32gui.GetWindowText(hwnd)
                    if app.lower() in t.lower():
                        lst.append(hwnd)
            win32gui.EnumWindows(_enum_title, matches)
            return matches[0] if matches else None
        return None

    # -- physical actions delegated to ljqCtrl ------------------------------

    def click(self, *, element=None, x=None, y=None, button="left",
              click_count=1, modifiers=None, delivery_mode=None, bring_to_front=False):
        _ensure_ljq()
        tx, ty = (x or 0), (y or 0)

        # Bring window to front if requested
        if bring_to_front and element:
            hwnd = self._find_hwnd(app=element.get("appName"))
            if hwnd:
                _ljqCtrl.Activate(hwnd)

        # Apply modifier keys (e.g. Ctrl+Click)
        if modifiers:
            for mod in modifiers:
                vk = _ljqCtrl.VK_CODE.get(mod.lower())
                if vk:
                    import win32api
                    win32api.keybd_event(vk, 0, 0, 0)

        # Physical click via ljqCtrl (with pixel-diff verification)
        cur = _ljqCtrl.win32api.GetCursorPos()
        _ljqCtrl.SetCursorPos((tx, ty))
        for _ in range(click_count):
            if button == "right":
                _ljqCtrl.win32api.mouse_event(_ljqCtrl.win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0)
                time.sleep(0.05)
                _ljqCtrl.win32api.mouse_event(_ljqCtrl.win32con.MOUSEEVENTF_RIGHTUP, 0, 0)
            elif button == "middle":
                _ljqCtrl.win32api.mouse_event(_ljqCtrl.win32con.MOUSEEVENTF_MIDDLEDOWN, 0, 0)
                time.sleep(0.05)
                _ljqCtrl.win32api.mouse_event(_ljqCtrl.win32con.MOUSEEVENTF_MIDDLEUP, 0, 0)
            else:
                _ljqCtrl.MouseClick()
            time.sleep(0.05)

        # Release modifiers
        if modifiers:
            for mod in reversed(modifiers):
                vk = _ljqCtrl.VK_CODE.get(mod.lower())
                if vk:
                    import win32con
                    _ljqCtrl.win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

        _ljqCtrl.SetCursorPos(cur)  # restore cursor
        return ActionResult(ok=True, action="click",
            message=f"click ({tx},{ty}) button={button} count={click_count}",
            path="win32_ljqCtrl")

    def drag(self, *, start_x=None, start_y=None, end_x=None, end_y=None,
             from_xy=None, to_xy=None, button="left",
             modifiers=None, delivery_mode=None, bring_to_front=False):
        _ensure_ljq()
        fx, fy = from_xy or (start_x or 0, start_y or 0)
        tx, ty = to_xy or (end_x or 0, end_y or 0)
        if modifiers:
            for mod in modifiers:
                vk = _ljqCtrl.VK_CODE.get(mod.lower())
                if vk:
                    _ljqCtrl.win32api.keybd_event(vk, 0, 0, 0)
        cur = _ljqCtrl.win32api.GetCursorPos()
        _ljqCtrl.SetCursorPos((fx, fy))
        ev_down = _ljqCtrl.win32con.MOUSEEVENTF_LEFTDOWN
        if button == "right":
            ev_down = _ljqCtrl.win32con.MOUSEEVENTF_RIGHTDOWN
        _ljqCtrl.win32api.mouse_event(ev_down, 0, 0)
        time.sleep(0.1)
        self._smooth_move(fx, fy, tx, ty)
        time.sleep(0.1)
        ev_up = _ljqCtrl.win32con.MOUSEEVENTF_LEFTUP
        if button == "right":
            ev_up = _ljqCtrl.win32con.MOUSEEVENTF_RIGHTUP
        _ljqCtrl.win32api.mouse_event(ev_up, 0, 0)
        if modifiers:
            for m in reversed(modifiers):
                vk = _ljqCtrl.VK_CODE.get(m.lower())
                if vk:
                    _ljqCtrl.win32api.keybd_event(vk, 0, _ljqCtrl.win32con.KEYEVENTF_KEYUP, 0)
        return ActionResult(ok=True, action="drag",
            message=f"drag ({fx},{fy})->({tx},{ty})", path="win32_ljqCtrl")

    def _smooth_move(self, x1, y1, x2, y2, duration=0.5):
        """Smooth cursor movement with comet trail overlay."""
        import subprocess
        _ensure_ljq()
        if x1 == x2 and y1 == y2:
            return
        
        trail_script = os.path.join(os.path.dirname(__file__), "show_trail.py")
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, trail_script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except Exception:
            pass

        steps = max(8, int(duration / 0.005))
        for i in range(1, steps + 1):
            t = i / steps
            cx = int(x1 + (x2 - x1) * t)
            cy = int(y1 + (y2 - y1) * t)
            _ljqCtrl.SetCursorPos((cx, cy))
            if proc and proc.stdin:
                try:
                    proc.stdin.write(f"{cx},{cy}\n")
                    proc.stdin.flush()
                except Exception:
                    pass
            time.sleep(0.005)

        if proc and proc.stdin:
            try:
                proc.stdin.write("done\n")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass

    def scroll(self, *, direction="down", amount=3, element=None,
               x=None, y=None, modifiers=None, delivery_mode=None, bring_to_front=False):
        _ensure_ljq()
        if x is not None and y is not None:
            _ljqCtrl.SetCursorPos((x, y))
        delta = amount * 120
        if direction == "down":
            delta = -delta
        elif direction in ("left", "right"):
            _ljqCtrl.win32api.mouse_event(
                _ljqCtrl.win32con.MOUSEEVENTF_HWHEEL, 0, 0,
                delta if direction == "right" else -delta, 0)
            return ActionResult(ok=True, action="scroll", path="win32_ljqCtrl")
        _ljqCtrl.win32api.mouse_event(_ljqCtrl.win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        return ActionResult(ok=True, action="scroll",
            message=f"scroll {direction}x{amount}", path="win32_ljqCtrl")

    def type_text(self, text, *, delivery_mode=None, bring_to_front=False):
        """Type text via pyperclip paste (ljqCtrl style, avoids char-by-char issues)."""
        _ensure_ljq()
        try:
            import pyperclip
            pyperclip.copy(text)
            time.sleep(0.1)
            _ljqCtrl.Press("ctrl+v")
            time.sleep(0.1)
            return ActionResult(ok=True, action="type",
                message=f"pasted {len(text)} chars via clipboard", path="win32_ljqCtrl")
        except ImportError:
            # Fallback: type per-char for simple text
            typed = 0
            for ch in text:
                if ch == "\n":
                    _ljqCtrl.Press("enter")
                    typed += 1
                    continue
                upper = ch.isupper()
                vk = _ljqCtrl.VK_CODE.get(ch.lower())
                if vk is None:
                    continue
                if upper:
                    _ljqCtrl.win32api.keybd_event(0x10, 0, 0, 0)
                _ljqCtrl.win32api.keybd_event(vk, 0, 0, 0)
                time.sleep(0.01)
                _ljqCtrl.win32api.keybd_event(vk, 0, _ljqCtrl.win32con.KEYEVENTF_KEYUP, 0)
                if upper:
                    _ljqCtrl.win32api.keybd_event(0x10, 0, _ljqCtrl.win32con.KEYEVENTF_KEYUP, 0)
                typed += 1
                time.sleep(0.005)
            return ActionResult(ok=True, action="type",
                message=f"typed {typed} chars (char-by-char)", path="win32_ljqCtrl")

    def key(self, keys, *, delivery_mode=None, bring_to_front=False):
        """Key combo via ljqCtrl.Press (e.g. 'ctrl+c', 'alt+tab')."""
        _ensure_ljq()
        _ljqCtrl.Press(keys)
        return ActionResult(ok=True, action="key",
            message=f"key {keys}", path="win32_ljqCtrl")

    def list_apps(self):
        """List visible windows."""
        _ensure_ljq()
        import win32gui, ctypes
        seen = {}
        def _enum(hwnd, d):
            if not isinstance(d, dict):
                return
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            pid = ctypes.c_uint()
            ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value not in d:
                d[pid.value] = {"pid": pid.value, "title": title, "hwnd": hwnd}
        win32gui.EnumWindows(_enum, seen)
        return list(seen.values())

    def focus_app(self, app, raise_window=False):
        """Focus a window by title substring."""
        _ensure_ljq()
        hwnd = self._find_hwnd(app=app)
        if not hwnd:
            return ActionResult(ok=False, action="focus_app",
                message=f"window not found: {app}", path="win32_ljqCtrl")
        if raise_window:
            _ljqCtrl.Activate(hwnd)
        return ActionResult(ok=True, action="focus_app",
            message=f"focused: {app}", path="win32_ljqCtrl")

    def set_value(self, value, element=None):
        return ActionResult(ok=False, action="set_value",
            message="set_value not supported in Win32NativeBackend (use type_text)",
            path="win32_ljqCtrl")
