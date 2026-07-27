
import sys
import json
import tkinter as tk
import time
import threading

def read_stdin(canvas, root, points, state):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "done":
            state["done"] = True
            break
        try:
            x, y = map(int, line.split(','))
            with points["lock"]:
                points["list"].append((x, y))
        except Exception as e:
            pass

def update_canvas(canvas, root, points, state):
    with points["lock"]:
        pts = list(points["list"])
        
    canvas.delete("all")
    n = len(pts)
    if n >= 2:
        for i in range(1, n):
            ratio = i / n
            width = max(2, int(15 * ratio))
            # Orange color gradient (from tail red-orange to head orange)
            r = 255
            g = int(120 * ratio)
            color = f'#{r:02x}{g:02x}00'
            canvas.create_line(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1],
                               fill=color, width=width, capstyle='round', joinstyle='round')
            
        # Draw head circle
        hx, hy = pts[-1]
        canvas.create_oval(hx-10, hy-10, hx+10, hy+10, fill='#ff6600', outline='')

    if not state["done"]:
        root.after(30, update_canvas, canvas, root, points, state)
    else:
        # Start persistence stage: keep it for 2 seconds
        root.after(2000, start_retraction, canvas, root, points)

def start_retraction(canvas, root, points):
    # Spring retract animation: shrink from tail to head
    pts = list(points["list"]) # local copy of the full final path
    
    # We will step-by-step shrink from index 0 towards end
    def animate_step(step_idx):
        nonlocal pts
        if len(pts) <= 2:
            root.destroy()
            return
            
        # Shrink the tail: remove the first 10% of points
        remove = max(1, len(pts) // 10)
        pts = pts[remove:]
        
        canvas.delete("all")
        n = len(pts)
        if n >= 2:
            for i in range(1, n):
                ratio = i / n
                # Fade the width as the spring retracts
                width = max(1, int(15 * ratio * (1.0 - step_idx/30.0)))
                r = 255
                g = int(120 * ratio)
                color = f'#{r:02x}{g:02x}00'
                canvas.create_line(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1],
                                   fill=color, width=width, capstyle='round', joinstyle='round')
            hx, hy = pts[-1]
            canvas.create_oval(hx-8, hy-8, hx+8, hy+8, fill='#ff6600', outline='')
            
        if step_idx < 30 and len(pts) > 2:
            root.after(30, animate_step, step_idx + 1)
        else:
            root.destroy()

    animate_step(0)

def main():
    points = {"list": [], "lock": threading.Lock()}
    state = {"done": False}

    root = tk.Tk()
    root.title('HermesTrail')
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-transparentcolor', '#010101')
    root.config(bg='#010101')
    
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    root.wm_attributes('-disabled', True)
    # Make window click-through: WS_EX_TRANSPARENT
    import ctypes as _ct
    def _set_clickthrough():
        # MUST use FindWindowW for our own window, not foreground
        hwnd = _ct.windll.user32.FindWindowW(None, "HermesTrail")
        if not hwnd:
            hwnd = _ct.windll.user32.GetForegroundWindow()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        style = _ct.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        _ct.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    root.after(100, _set_clickthrough)

    canvas = tk.Canvas(root, bg='#010101', highlightthickness=0, bd=0)
    canvas.pack(fill='both', expand=True)

    # Thread to read stdin from parent process
    t = threading.Thread(target=read_stdin, args=(canvas, root, points, state), daemon=True)
    t.start()

    # Start Tkinter redraw loop
    root.after(30, update_canvas, canvas, root, points, state)
    root.mainloop()

if __name__ == '__main__':
    main()
