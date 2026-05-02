import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import json
import queue
import re
from pathlib import Path
import serial
import serial.tools.list_ports
import threading
import time

# ── Theme ────────────────────────────────────────────────────────────────────
BG       = "#0f1117"
PANEL    = "#1a1d27"
CARD     = "#22263a"
ACCENT   = "#00d4aa"
ACCENT2  = "#ff6b6b"
ACCENT3  = "#ffd166"
TEXT     = "#e8eaf0"
MUTED    = "#6b7280"
BORDER   = "#2e3350"
BTN_TEXT = "#0f1117"

FONT_TITLE = ("Courier New", 18, "bold")
FONT_LABEL = ("Courier New", 10, "bold")
FONT_MONO  = ("Courier New", 10)
FONT_SMALL = ("Courier New", 9)
FONT_BIG   = ("Courier New", 13, "bold")

# ════════════════════════════════════════════════════════════════════════════
#  STATE
# ════════════════════════════════════════════════════════════════════════════
ser         = None
connected   = False
ser_lock    = threading.Lock()
state_lock  = threading.Lock()
motion_lock = threading.RLock()
current_x   = 0
current_y   = 0
pen_is_down = False
calibration_ready = False
_loading_state    = False
current_servo_angle = 90
draw_in_progress   = False
_draw_cancel       = False
_last_preview_redraw = 0.0
preview_origin_override = None
preview_job = {
    "label": "",
    "spec": None,
    "planned": [],
    "segments_total": 0,
    "segments_done": 0,
}
STATE_FILE = Path(__file__).with_name("control_state.json")
DEFAULT_DRAW_MARGIN = 5
response_queue = queue.Queue()
ACK_RE = re.compile(r"^OK X=(-?\d+) Y=(-?\d+) PEN=(UP|DOWN)$")

# ════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL SERIAL
# ════════════════════════════════════════════════════════════════════════════
def _raw_send(cmd):
    global ser, connected
    if not connected or ser is None:
        log(f"[NOT CONNECTED]  {cmd}", color=ACCENT2)
        return False
    try:
        with ser_lock:
            ser.write((cmd.strip() + "\n").encode())
        log(f"  >> {cmd}", color=ACCENT)
        return True
    except Exception as e:
        log(f"[SERIAL ERROR] {e}", color=ACCENT2)
        return False


def log(msg, color=TEXT):
    """Thread-safe log — always schedules on main thread."""
    def _do():
        console.config(state="normal")
        console.insert("end", msg + "\n", color)
        console.tag_config(color, foreground=color)
        console.see("end")
        console.config(state="disabled")
    try:
        root.after(0, _do)
    except Exception:
        pass


def _refresh_pos_label():
    if "pos_lbl" not in globals():
        return
    _, lim_x, lim_y = _get_limits(force_enforce=True)
    pos_lbl.config(text=f"X: {current_x}/{lim_x}  Y: {current_y}/{lim_y}")


def _refresh_pen_label():
    if "pen_lbl" not in globals():
        return
    pen_lbl.config(text=f"Pen: {'DOWN' if pen_is_down else 'UP'}  Servo: {current_servo_angle}\N{DEGREE SIGN}")


def _refresh_preview_status():
    if "preview_status_var" not in globals():
        return
    total = preview_job.get("segments_total", 0)
    done  = preview_job.get("segments_done", 0)
    label = preview_job.get("label") or "No preview loaded"
    if total:
        preview_status_var.set(f"{label}  Segments: {done}/{total}")
    else:
        preview_status_var.set(label)


def _refresh_control_visibility():
    if "pen_action_row" not in globals() or "calib_action_row" not in globals():
        return
    if _calib_active:
        # Calibrate mode: show SET UP / SET DOWN / nudge; hide MOVE UP/DOWN
        if pen_action_row.winfo_manager():
            pen_action_row.pack_forget()
        if not calib_action_row.winfo_manager():
            calib_action_row.pack(padx=10, pady=(0, 4), fill="x")
        if "pcal_card" in globals() and not pcal_card.winfo_manager():
            pcal_card.pack(fill="x", pady=4)
        if "pcal_angles_row" in globals() and not pcal_angles_row.winfo_manager():
            pcal_angles_row.pack(fill="x", padx=10, pady=(4, 2))
        if "pcal_dn_row" in globals() and not pcal_dn_row.winfo_manager():
            pcal_dn_row.pack(fill="x", padx=10, pady=(0, 2))
        if "bounds_set_row" in globals() and not bounds_set_row.winfo_manager():
            bounds_set_row.pack(padx=10, pady=(0, 4), fill="x")
    else:
        # Normal mode: show MOVE UP/DOWN; hide calibrate controls
        if calib_action_row.winfo_manager():
            calib_action_row.pack_forget()
        if not pen_action_row.winfo_manager():
            pen_action_row.pack(padx=10, pady=(0, 8), fill="x")
        if "pcal_card" in globals() and pcal_card.winfo_manager():
            pcal_card.pack_forget()
        if "pcal_angles_row" in globals() and pcal_angles_row.winfo_manager():
            pcal_angles_row.pack_forget()
        if "pcal_dn_row" in globals() and pcal_dn_row.winfo_manager():
            pcal_dn_row.pack_forget()
        if "bounds_set_row" in globals() and bounds_set_row.winfo_manager():
            bounds_set_row.pack_forget()


def _set_draw_busy(active, label="DRAW"):
    global draw_in_progress
    draw_in_progress = bool(active)
    # All widget updates MUST be scheduled on the main thread to avoid Tk deadlock
    def _ui():
        if "confirm_draw_btn" in globals():
            confirm_draw_btn.config(state=("disabled" if active else "normal"))
        if "clear_preview_btn" in globals():
            clear_preview_btn.config(state=("disabled" if active else "normal"))
        if "preview_status_var" in globals():
            if active:
                preview_status_var.set(f"{label} running...")
            else:
                _refresh_preview_status()
    root.after(0, _ui)


def _manual_motion_allowed(label="MOVE"):
    if draw_in_progress:
        log(f"[{label}] Drawing is in progress", color=ACCENT2)
        return False
    return True


def _set_servo_angle_display(angle=None):
    global current_servo_angle
    if angle is None:
        angle = _safe_int(pcal_dn_var.get() if pen_is_down else pcal_up_var.get(), current_servo_angle)
    current_servo_angle = max(0, min(180, _safe_int(angle, current_servo_angle)))
    root.after(0, _refresh_pen_label)


def _update_pen_calibration_vars(up=None, down=None, active_angle=None):
    def _apply():
        if up is not None:
            pcal_up_var.set(str(max(0, min(180, _safe_int(up, 90)))))
        if down is not None:
            pcal_dn_var.set(str(max(0, min(180, _safe_int(down, 30)))))
        _set_servo_angle_display(active_angle)
    root.after(0, _apply)


def _safe_int(value, default=0, minimum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _safe_float(value, default=0.0, minimum=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _get_limits(force_enforce=None, enforce=None, lim_x=None, lim_y=None):
    if enforce is None:
        enforce = enforce_bounds_var.get() if force_enforce is None else bool(force_enforce)
    if lim_x is None:
        lim_x = max(1, _safe_int(bound_x_var.get(), 500, 1))
    if lim_y is None:
        lim_y = max(1, _safe_int(bound_y_var.get(), 500, 1))
    return enforce, lim_x, lim_y


def _snapshot_ui_settings():
    """Read all tkinter vars — call only on main thread."""
    return {
        "swap":    swap_xy_var.get(),
        "inv_x":   inv_x_var.get(),
        "inv_y":   inv_y_var.get(),
        "enforce": enforce_bounds_var.get(),
        "lim_x":   max(1, _safe_int(bound_x_var.get(), 500, 1)),
        "lim_y":   max(1, _safe_int(bound_y_var.get(), 500, 1)),
        "step":    max(1, _safe_int(step_var.get(), 20, 1)),
        "speed":   max(1, _safe_int(speed_var.get(), 30, 1)),
    }


def _current_position():
    with state_lock:
        return current_x, current_y


def _logical_to_physical(logical_x, logical_y, swap=None, inv_x=None, inv_y=None):
    if swap  is None: swap  = swap_xy_var.get()
    if inv_x is None: inv_x = inv_x_var.get()
    if inv_y is None: inv_y = inv_y_var.get()
    phys_x, phys_y = (logical_y, logical_x) if swap else (logical_x, logical_y)
    if inv_x: phys_x = -phys_x
    if inv_y: phys_y = -phys_y
    return phys_x, phys_y


def _physical_to_logical(phys_x, phys_y, swap=None, inv_x=None, inv_y=None):
    if swap  is None: swap  = swap_xy_var.get()
    if inv_x is None: inv_x = inv_x_var.get()
    if inv_y is None: inv_y = inv_y_var.get()
    if inv_x: phys_x = -phys_x
    if inv_y: phys_y = -phys_y
    return (phys_y, phys_x) if swap else (phys_x, phys_y)


def _clear_response_queue():
    while True:
        try:
            response_queue.get_nowait()
        except queue.Empty:
            return


def _clamp_position_if_enforced(logical_x, logical_y):
    """Clamp position to 0..max when bounds enforcement is active.
    Returns (clamped_x, clamped_y, was_clamped)."""
    try:
        enforce = enforce_bounds_var.get()
    except Exception:
        return logical_x, logical_y, False
    if not enforce:
        return logical_x, logical_y, False
    lim_x = max(1, _safe_int(bound_x_var.get(), 500, 1))
    lim_y = max(1, _safe_int(bound_y_var.get(), 500, 1))
    cx = max(0, min(lim_x, logical_x))
    cy = max(0, min(lim_y, logical_y))
    clamped = (cx != logical_x) or (cy != logical_y)
    return cx, cy, clamped


def _set_local_state_from_physical(phys_x, phys_y, pen_down, persist=True):
    global current_x, current_y, pen_is_down
    logical_x, logical_y = _physical_to_logical(phys_x, phys_y)
    # Clamp to valid bounds when enforcement is on
    cx, cy, was_clamped = _clamp_position_if_enforced(logical_x, logical_y)
    if was_clamped:
        log(f"[BOUNDS] Position clamped: ({logical_x},{logical_y}) → ({cx},{cy})", color=ACCENT3)
    with state_lock:
        current_x   = cx
        current_y   = cy
        pen_is_down = bool(pen_down)
    _set_servo_angle_display()
    if persist:
        _persist_state(log_error=False)
    root.after(0, _refresh_pos_label)
    root.after(0, _refresh_pen_label)
    root.after(0, _redraw_preview_canvas)


def _parse_ack_line(line):
    match = ACK_RE.fullmatch(line.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), match.group(3) == "DOWN"


def _send_command_wait(cmd, timeout=15):
    global ser, connected
    if not connected or ser is None:
        log(f"[NOT CONNECTED]  {cmd}", color=ACCENT2)
        return None
    try:
        _clear_response_queue()
        with ser_lock:
            ser.write((cmd.strip() + "\n").encode())
        log(f"  >> {cmd}", color=ACCENT)
    except Exception as exc:
        log(f"[SERIAL ERROR] {exc}", color=ACCENT2)
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check if connection died while waiting
        if not connected or ser is None:
            log(f"[DISCONNECTED] Lost connection during {cmd}", color=ACCENT2)
            return None
        remaining = max(0.05, deadline - time.time())
        try:
            line = response_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        parsed = _parse_ack_line(line)
        if parsed is not None:
            return parsed
        if line.startswith("ERR "):
            log(f"[CONTROLLER] {line}", color=ACCENT2)
            return None
        # Skip informational lines (PEN_UP=, NUDGE=, SPEED=, etc.) and keep waiting for OK
        continue

    log(f"[TIMEOUT] No controller confirmation for {cmd}", color=ACCENT2)
    return None


def _persist_state(log_error=False):
    """Thread-safe: snapshots plain values then schedules JSON write on main thread."""
    if _loading_state:
        return
    with state_lock:
        pos_x, pos_y, pen_down = current_x, current_y, pen_is_down
    calib = calibration_ready

    def _write_on_main():
        if _loading_state:
            return
        payload = {
            "version": 2,
            "calibrated":     calib,
            "bounds":         {"x": max(1, _safe_int(bound_x_var.get(), 500, 1)),
                               "y": max(1, _safe_int(bound_y_var.get(), 500, 1))},
            "position":       {"x": pos_x, "y": pos_y},
            "pen_state_down": pen_down,
            "enforce_bounds": bool(enforce_bounds_var.get()),
            "swap_xy":        bool(swap_xy_var.get()),
            "invert_x":       bool(inv_x_var.get()),
            "invert_y":       bool(inv_y_var.get()),
            "step":           max(1, _safe_int(step_var.get(), 20, 1)),
            "speed":          max(1, _safe_int(speed_var.get(), 30, 1)),
            "pen_up":         _safe_int(pcal_up_var.get(), 90),
            "pen_down":       _safe_int(pcal_dn_var.get(), 30),
            "shape_size":     max(5, _safe_int(size_var.get(), 30, 5)),
            "rings":          max(1, _safe_int(rings_var.get(), 3, 1)),
        }
        try:
            STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            if log_error:
                log(f"[STATE] Save failed: {exc}", color=ACCENT2)

    root.after(0, _write_on_main)


def _apply_calibration_ui_state():
    if "calib_btn" not in globals():
        return
    if _calib_active:
        calib_btn.config(text="SAVE CALIB", bg=ACCENT2, fg=BTN_TEXT)
    else:
        calib_btn.config(text="CALIBRATE", bg=BORDER, fg=TEXT)
    _refresh_control_visibility()


def _load_state():
    global calibration_ready, _loading_state, current_x, current_y, pen_is_down, current_servo_angle
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"[STATE] Load failed: {exc}", color=ACCENT2)
        return

    bounds   = data.get("bounds", {})
    position = data.get("position", {})
    _loading_state = True
    try:
        bound_x_var.set(str(max(1, _safe_int(bounds.get("x"), 500, 1))))
        bound_y_var.set(str(max(1, _safe_int(bounds.get("y"), 500, 1))))
        enforce_bounds_var.set(bool(data.get("enforce_bounds", False)))
        swap_xy_var.set(bool(data.get("swap_xy", False)))
        inv_x_var.set(bool(data.get("invert_x", False)))
        inv_y_var.set(bool(data.get("invert_y", False)))
        step_var.set(str(max(1, _safe_int(data.get("step"), 20, 1))))
        speed_var.set(str(max(1, _safe_int(data.get("speed"), 30, 1))))
        pcal_up_var.set(str(_safe_int(data.get("pen_up"), 90)))
        pcal_dn_var.set(str(_safe_int(data.get("pen_down"), 30)))
        size_var.set(str(max(5, _safe_int(data.get("shape_size"), 30, 5))))
        rings_var.set(max(1, _safe_int(data.get("rings"), 3, 1)))
        calibration_ready = bool(data.get("calibrated", False))
        raw_x   = _safe_int(position.get("x"), 0)
        raw_y   = _safe_int(position.get("y"), 0)
        # Clamp loaded position to valid bounds (0..max)
        lim_x_val = max(1, _safe_int(bounds.get("x"), 500, 1))
        lim_y_val = max(1, _safe_int(bounds.get("y"), 500, 1))
        if bool(data.get("enforce_bounds", False)):
            current_x = max(0, min(lim_x_val, raw_x))
            current_y = max(0, min(lim_y_val, raw_y))
            if current_x != raw_x or current_y != raw_y:
                log(f"[STATE] Position clamped on load: ({raw_x},{raw_y}) → ({current_x},{current_y})", color=ACCENT3)
        else:
            current_x = raw_x
            current_y = raw_y
        pen_is_down = bool(data.get("pen_state_down", data.get("pen_down_state", False)))
        current_servo_angle = _safe_int(data.get("pen_down"), 30) if pen_is_down else _safe_int(data.get("pen_up"), 90)
    finally:
        _loading_state = False

    _apply_calibration_ui_state()
    _refresh_control_visibility()
    root.after(0, _refresh_pos_label)
    root.after(0, _refresh_pen_label)
    if calibration_ready:
        log(f"[STATE] Calibration restored  Max X:{bound_x_var.get()}  Max Y:{bound_y_var.get()}", color=ACCENT3)
    log(f"[STATE] Last known position restored  X:{current_x}  Y:{current_y}", color=ACCENT3)


def _bind_persisted_vars():
    def _save(*_):
        _persist_state(log_error=False)
    for var in [step_var, speed_var, size_var, rings_var, swap_xy_var, inv_x_var,
                inv_y_var, enforce_bounds_var, bound_x_var, bound_y_var,
                pcal_up_var, pcal_dn_var]:
        var.trace_add("write", _save)


# ════════════════════════════════════════════════════════════════════════════
#  TRACKED MOVEMENT ENGINE
# ════════════════════════════════════════════════════════════════════════════
def _tracked_send(logical_axis, logical_steps, force_enforce=None):
    if logical_steps == 0:
        return 0

    with motion_lock:
        with state_lock:
            enforce, lim_x, lim_y = _get_limits(force_enforce=force_enforce)
            start_x, start_y = current_x, current_y

            steps = logical_steps
            if enforce:
                if logical_axis == "X":
                    target = max(0, min(lim_x, start_x + steps))
                    steps  = target - start_x
                else:
                    target = max(0, min(lim_y, start_y + steps))
                    steps  = target - start_y

            if steps == 0:
                return 0

            target_x = start_x + (steps if logical_axis == "X" else 0)
            target_y = start_y + (steps if logical_axis == "Y" else 0)
            phys_start_x,  phys_start_y  = _logical_to_physical(start_x, start_y)
            phys_target_x, phys_target_y = _logical_to_physical(target_x, target_y)

        phys_dx = phys_target_x - phys_start_x
        phys_dy = phys_target_y - phys_start_y
        cmd = (f"M{phys_dx},{phys_dy}" if phys_dx and phys_dy
               else (f"X{phys_dx}" if phys_dx else f"Y{phys_dy}"))
        ack = _send_command_wait(cmd)
        if ack is None:
            return 0
        _set_local_state_from_physical(*ack)

    end_x, end_y = _current_position()
    return (end_x - start_x) if logical_axis == "X" else (end_y - start_y)


def _tracked_diagonal(logical_x_steps, logical_y_steps, force_enforce=None):
    if logical_x_steps == 0 and logical_y_steps == 0:
        return 0, 0

    with motion_lock:
        with state_lock:
            enforce, lim_x, lim_y = _get_limits(force_enforce=force_enforce)
            start_x, start_y = current_x, current_y
            sx, sy = logical_x_steps, logical_y_steps

            if enforce:
                ratio = 1.0
                if sx != 0:
                    tx = start_x + sx
                    if tx > lim_x: ratio = min(ratio, (lim_x - start_x) / sx)
                    elif tx < 0:   ratio = min(ratio, (0 - start_x) / sx)
                if sy != 0:
                    ty = start_y + sy
                    if ty > lim_y: ratio = min(ratio, (lim_y - start_y) / sy)
                    elif ty < 0:   ratio = min(ratio, (0 - start_y) / sy)
                ratio = max(0.0, min(1.0, ratio))
                sx = int(round(sx * ratio))
                sy = int(round(sy * ratio))
                tx = max(0, min(lim_x, start_x + sx))
                ty = max(0, min(lim_y, start_y + sy))
                sx = tx - start_x
                sy = ty - start_y

            if sx == 0 and sy == 0:
                return 0, 0

            target_x = start_x + sx
            target_y = start_y + sy
            phys_start_x,  phys_start_y  = _logical_to_physical(start_x, start_y)
            phys_target_x, phys_target_y = _logical_to_physical(target_x, target_y)

        ack = _send_command_wait(f"M{phys_target_x - phys_start_x},{phys_target_y - phys_start_y}")
        if ack is None:
            return 0, 0
        _set_local_state_from_physical(*ack)

    end_x, end_y = _current_position()
    return end_x - start_x, end_y - start_y


def _set_pen(down, label="PEN"):
    desired = bool(down)
    with motion_lock:
        with state_lock:
            if pen_is_down == desired:
                return True
        ack = _send_command_wait("PD" if desired else "PU", timeout=10)
        if ack is None:
            log(f"[{label}] Could not set pen {'DOWN' if desired else 'UP'}", color=ACCENT2)
            return False
        _set_local_state_from_physical(*ack)
    log(f"[{label}] Pen {'DOWN' if desired else 'UP'}", color=ACCENT3)
    return True


def _send_passthrough_command(cmd, label="RAW", timeout=20):
    with motion_lock:
        ack = _send_command_wait(cmd, timeout=timeout)
        if ack is None:
            return False
        _set_local_state_from_physical(*ack)
    log(f"[{label}] Controller state synced", color=ACCENT3)
    return True


def emergency_stop():
    """Cancel any in-progress draw, try to raise pen, then hard-stop."""
    global _draw_cancel
    _draw_cancel = True
    log("[STOP] Emergency stop triggered!", color=ACCENT2)

    def _do_stop():
        # Try to raise pen gracefully first
        try:
            ack = _send_command_wait("PU", timeout=3)
            if ack:
                _set_local_state_from_physical(*ack)
                log("[STOP] Pen raised successfully", color=ACCENT3)
            else:
                log("[STOP] Pen-up timed out, hard-stopping", color=ACCENT2)
        except Exception:
            pass
        # Hard stop regardless
        _send_passthrough_command("STOP", label="STOP", timeout=5)

    _run_in_thread(_do_stop)


def return_to_zero(label="ZERO"):
    with state_lock:
        tx, ty = current_x, current_y
    if tx == 0 and ty == 0:
        log(f"[{label}] Already at origin", color=ACCENT3)
        return True

    log(f"[{label}] Homing from X:{tx}  Y:{ty}", color=ACCENT3)
    if not _set_pen(False, label=label):
        return False
    if not _send_passthrough_command("HOME", label=label, timeout=30):
        return False
    with state_lock:
        final_x, final_y = current_x, current_y
    log(f"[{label}] Origin reached  X:{final_x}  Y:{final_y}", color=ACCENT3)
    return True


# ════════════════════════════════════════════════════════════════════════════
#  PUBLIC GUI ACTIONS  — every button handler that touches serial runs in a
#  daemon thread so the Tk main thread is NEVER blocked.
# ════════════════════════════════════════════════════════════════════════════
def _run_in_thread(fn, *args, **kwargs):
    threading.Thread(target=lambda: fn(*args, **kwargs), daemon=True).start()


def move(axis, direction):
    if not _manual_motion_allowed():
        return
    s = direction * max(1, _safe_int(step_var.get(), 20, 1))
    _run_in_thread(_tracked_send, axis, s)


def move_diagonal(dx, dy):
    if not _manual_motion_allowed():
        return
    s = max(1, _safe_int(step_var.get(), 20, 1))
    _run_in_thread(_tracked_diagonal, dx * s, dy * s)


def move_pen(down):
    """Move pen up/down — only allowed when not drawing."""
    if draw_in_progress:
        log("[PEN] Cannot move pen while drawing", color=ACCENT2)
        return
    with state_lock:
        already = pen_is_down == bool(down)
    if already:
        log(f"[PEN] Already {'DOWN' if down else 'UP'}", color=MUTED)
        return
    _run_in_thread(_set_pen, down)


def _move_to_absolute(target_x, target_y, force_enforce=True):
    _, lim_x, lim_y = _get_limits(force_enforce=force_enforce)
    target_x = max(0, min(lim_x, int(round(target_x))))
    target_y = max(0, min(lim_y, int(round(target_y))))
    start_x, start_y = _current_position()
    dx = target_x - start_x
    dy = target_y - start_y
    if dx == 0 and dy == 0:
        return True
    if dx != 0 and dy != 0:
        _tracked_diagonal(dx, dy, force_enforce=force_enforce)
    elif dx != 0:
        _tracked_send("X", dx, force_enforce=force_enforce)
    else:
        _tracked_send("Y", dy, force_enforce=force_enforce)
    return _current_position() == (target_x, target_y)


def _ensure_ready_for_draw(label):
    if _calib_active:
        log(f"[{label}] Finish calibration before drawing", color=ACCENT2)
        return False
    if draw_in_progress:
        log(f"[{label}] Another drawing is already running", color=ACCENT2)
        return False
    if not calibration_ready:
        log(f"[{label}] Save calibration first so drawing has real limits", color=ACCENT2)
        return False
    return True


def _coerce_paths(spec):
    raw_paths = spec.get("paths")
    if raw_paths is None and "points" in spec:
        raw_paths = [{"points": spec.get("points", []), "closed": spec.get("closed", False)}]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("JSON must contain 'paths' or 'points'")

    paths = []
    for idx, raw_path in enumerate(raw_paths, start=1):
        closed = False
        points_src = raw_path
        draw_path = True
        if isinstance(raw_path, dict):
            points_src = raw_path.get("points")
            closed = bool(raw_path.get("closed", False))
            if "draw" in raw_path:
                draw_path = bool(raw_path.get("draw"))
            elif "pen" in raw_path:
                draw_path = str(raw_path.get("pen", "down")).lower() != "up"
        if not isinstance(points_src, list) or len(points_src) < 2:
            raise ValueError(f"Path {idx} needs at least 2 points")
        points = []
        for point in points_src:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"Path {idx} has an invalid point")
            points.append((float(point[0]), float(point[1])))
        if closed and points[0] != points[-1]:
            points.append(points[0])
        paths.append({"points": points, "draw": draw_path})
    return paths


def _fit_paths_to_area(paths, spec):
    _, lim_x, lim_y = _get_limits(force_enforce=True)
    area    = spec.get("area") or {}
    area_x  = max(0, _safe_int(area.get("x"), 0, 0))
    area_y  = max(0, _safe_int(area.get("y"), 0, 0))
    area_w  = max(1, _safe_int(area.get("width"),  lim_x - area_x, 1))
    area_h  = max(1, _safe_int(area.get("height"), lim_y - area_y, 1))
    area_right  = min(lim_x, area_x + area_w)
    area_bottom = min(lim_y, area_y + area_h)

    margin = _safe_int(spec.get("margin"), DEFAULT_DRAW_MARGIN, 0)
    x_min  = max(0, area_x + margin)
    y_min  = max(0, area_y + margin)
    x_max  = min(lim_x, area_right  - margin)
    y_max  = min(lim_y, area_bottom - margin)
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("Requested draw area is smaller than the safety margin")

    xs = [x for path in paths for x, _ in path["points"]]
    ys = [y for path in paths for _, y in path["points"]]
    src_min_x, src_max_x = min(xs), max(xs)
    src_min_y, src_max_y = min(ys), max(ys)
    src_w = src_max_x - src_min_x
    src_h = src_max_y - src_min_y
    dst_w = x_max - x_min
    dst_h = y_max - y_min

    fit         = str(spec.get("fit", "auto")).lower()
    keep_aspect = bool(spec.get("keep_aspect", True))

    max_scale_x = (dst_w / src_w) if src_w else 1.0
    max_scale_y = (dst_h / src_h) if src_h else 1.0

    if fit == "fill" and not keep_aspect:
        scale_x = max_scale_x if src_w else 1.0
        scale_y = max_scale_y if src_h else 1.0
    elif not keep_aspect:
        scale_x = min(1.0, max_scale_x) if fit == "auto" and src_w else max_scale_x
        scale_y = min(1.0, max_scale_y) if fit == "auto" and src_h else max_scale_y
    else:
        candidates = []
        if src_w: candidates.append(max_scale_x)
        if src_h: candidates.append(max_scale_y)
        scale = min(candidates) if candidates else 1.0
        if fit == "auto":
            scale = min(1.0, scale)
        scale_x = scale_y = scale

    scaled_min_x = src_min_x * scale_x
    scaled_max_x = src_max_x * scale_x
    scaled_min_y = src_min_y * scale_y
    scaled_max_y = src_max_y * scale_y
    first_x, first_y = paths[0]["points"][0]
    first_x *= scale_x
    first_y *= scale_y

    min_tx = x_min - scaled_min_x
    max_tx = x_max - scaled_max_x
    min_ty = y_min - scaled_min_y
    max_ty = y_max - scaled_max_y

    origin = spec.get("origin")
    if isinstance(origin, dict):
        tx = min(max(_safe_float(origin.get("x"), min_tx), min_tx), max_tx)
        ty = min(max(_safe_float(origin.get("y"), min_ty), min_ty), max_ty)
    else:
        align = str(spec.get("align", "start")).lower()
        if align == "center":
            tx = (min_tx + max_tx) / 2.0
            ty = (min_ty + max_ty) / 2.0
        elif align == "origin":
            tx = min_tx
            ty = min_ty
        else:
            tx = min(max(x_min - first_x, min_tx), max_tx)
            ty = min(max(y_min - first_y, min_ty), max_ty)

    planned = []
    for path in paths:
        planned_path = []
        for px, py in path["points"]:
            x = max(0, min(lim_x, int(round(px * scale_x + tx))))
            y = max(0, min(lim_y, int(round(py * scale_y + ty))))
            point = (x, y)
            if not planned_path or planned_path[-1] != point:
                planned_path.append(point)
        if len(planned_path) >= 2:
            planned.append({"points": planned_path, "draw": path.get("draw", True)})

    if not planned:
        raise ValueError("Drawing collapsed after fitting to the safe area")
    return planned


def _preview_spec_with_fit(spec, label="DRAW", update_editor=False, quiet=False):
    global preview_job, preview_origin_override
    if not _ensure_ready_for_draw(label):
        return False
    try:
        raw_paths = _coerce_paths(spec)
        preview_spec = json.loads(json.dumps(spec))
        if preview_origin_override is not None:
            preview_spec["origin"] = {
                "x": int(round(preview_origin_override[0])),
                "y": int(round(preview_origin_override[1])),
            }
        planned = _fit_paths_to_area(raw_paths, preview_spec)
    except ValueError as exc:
        log(f"[{label}] {exc}", color=ACCENT2)
        return False

    preview_job = {
        "label": label,
        "spec": preview_spec,
        "planned": planned,
        "segments_total": sum(max(0, len(path["points"]) - 1) for path in planned),
        "segments_done": 0,
    }
    if update_editor and "json_editor" in globals():
        json_editor.delete("1.0", "end")
        json_editor.insert("1.0", json.dumps(preview_spec, indent=2))
    _refresh_preview_status()
    root.after(0, _redraw_preview_canvas)
    if not quiet:
        log(f"[{label}] Preview ready. Confirm to start drawing.", color=ACCENT3)
    return True


def _clear_preview():
    global preview_job, preview_origin_override
    if draw_in_progress:
        return
    preview_origin_override = None
    preview_job = {"label": "", "spec": None, "planned": [], "segments_total": 0, "segments_done": 0}
    _refresh_preview_status()
    root.after(0, _redraw_preview_canvas)


def _preview_builtin_shape(prefix):
    global preview_origin_override
    preview_origin_override = None
    _preview_spec_with_fit(_make_builtin_shape_spec(prefix), label=f"SHAPE {prefix}")


def _commit_preview_draw():
    if draw_in_progress:
        return
    spec = preview_job.get("spec")
    label = preview_job.get("label") or "DRAW"
    if not spec:
        log("[PREVIEW] Load a shape or JSON preview first", color=ACCENT2)
        return
    _run_in_thread(_run_draw_spec, spec, label=label)


def _run_draw_spec(spec, label="DRAW"):
    global _draw_cancel, _last_preview_redraw
    if not _ensure_ready_for_draw(label):
        return False
    try:
        paths   = _coerce_paths(spec)
        planned = _fit_paths_to_area(paths, spec)
    except ValueError as exc:
        log(f"[{label}] {exc}", color=ACCENT2)
        return False

    auto_home      = bool(spec.get("auto_home", True))
    return_home    = bool(spec.get("return_home", False))
    segment_delay  = _safe_float(spec.get("segment_delay"), 0.05, 0.0)
    segment_count  = sum(max(0, len(path["points"]) - 1) for path in planned)
    log(f"[{label}] Planned {segment_count} safe segments", color=ACCENT3)
    preview_job["segments_total"] = segment_count
    preview_job["segments_done"] = 0
    preview_job["planned"] = planned
    preview_job["spec"] = spec
    preview_job["label"] = label
    root.after(0, _redraw_preview_canvas)
    root.after(0, _refresh_preview_status)

    _draw_cancel = False
    _last_preview_redraw = time.time()
    _set_draw_busy(True, label=label)
    try:
        # Auto-home before starting
        if auto_home:
            with motion_lock:
                if _draw_cancel:
                    log(f"[{label}] Cancelled before start", color=ACCENT2)
                    return False
                if not return_to_zero(label=label):
                    return False

        for path_idx, path in enumerate(planned):
            # Check cancel between each path (motion_lock released here)
            if _draw_cancel:
                log(f"[{label}] Cancelled at path {path_idx+1}/{len(planned)}", color=ACCENT2)
                _set_pen(False, label=label)
                return False
            if not connected and ser is not None:
                log(f"[{label}] Connection lost at path {path_idx+1}", color=ACCENT2)
                return False

            path_points = path["points"]
            draw_path = bool(path.get("draw", True))

            with motion_lock:
                if not _set_pen(False, label=label):
                    return False
                if not _move_to_absolute(*path_points[0], force_enforce=True):
                    log(f"[{label}] Could not reach safe start point", color=ACCENT2)
                    return False
                if draw_path and not _set_pen(True, label=label):
                    return False
                if segment_delay:
                    time.sleep(segment_delay)
                for point in path_points[1:]:
                    if _draw_cancel:
                        log(f"[{label}] Cancelled mid-path", color=ACCENT2)
                        _set_pen(False, label=label)
                        return False
                    if not _move_to_absolute(*point, force_enforce=True):
                        log(f"[{label}] Motion aborted before reaching {point}", color=ACCENT2)
                        return False
                    preview_job["segments_done"] += 1
                    # Throttle preview redraws: every 5 segments or every 2 seconds
                    now = time.time()
                    if (preview_job["segments_done"] % 5 == 0
                            or now - _last_preview_redraw >= 2.0
                            or preview_job["segments_done"] == segment_count):
                        _last_preview_redraw = now
                        root.after(0, _refresh_preview_status)
                        root.after(0, _redraw_preview_canvas)
                    if segment_delay:
                        time.sleep(segment_delay)
                if draw_path and not _set_pen(False, label=label):
                    return False

        if return_home:
            with motion_lock:
                if not return_to_zero(label=label):
                    return False
        else:
            _set_pen(False, label=label)
    finally:
        _draw_cancel = False
        _set_draw_busy(False, label=label)
        # Final canvas update
        root.after(0, _refresh_preview_status)
        root.after(0, _redraw_preview_canvas)

    log(f"[{label}] Complete", color=ACCENT3)
    return True


def _start_draw_spec(spec, label="DRAW"):
    _run_in_thread(_run_draw_spec, spec, label=label)
    return True


def _make_builtin_shape_spec(prefix, size=None, rect_height=None, rings=None):
    size       = max(1, _safe_int(size,  _safe_int(size_var.get(),  30, 1), 1))
    rings      = max(1, _safe_int(rings, _safe_int(rings_var.get(), 3,  1), 1))
    rect_height = max(1, _safe_int(rect_height, max(1, int(round(size * 0.6))), 1))

    if prefix == "SQ":
        paths = [{"closed": True, "points": [(0,0),(size,0),(size,size),(0,size)]}]
    elif prefix == "TR":
        paths = [{"closed": True, "points": [(0,0),(size,0),(size/2,size)]}]
    elif prefix == "DM":
        paths = [{"closed": True, "points": [(0,size/2),(size/2,0),(size,size/2),(size/2,size)]}]
    elif prefix == "RC":
        paths = [{"closed": True, "points": [(0,0),(size,0),(size,rect_height),(0,rect_height)]}]
    elif prefix == "ZZ":
        paths = [{"points": [(0,0),(size,size),(2*size,0),(3*size,size),(4*size,0)]}]
    elif prefix == "SP":
        points = [(0, 0)]
        x = y = 0
        step = max(2, size)
        for ring in range(1, rings + 1):
            x += step * ring;     points.append((x, y))
            y += step * ring;     points.append((x, y))
            x -= step * (ring+1); points.append((x, y))
            y -= step * (ring+1); points.append((x, y))
        paths = [{"points": points}]
    else:
        raise ValueError(f"Unknown shape prefix: {prefix}")

    return {"auto_home": True, "align": "start", "fit": "auto",
            "margin": DEFAULT_DRAW_MARGIN, "paths": paths}


def send_shape(prefix):
    _preview_builtin_shape(prefix)


def _dispatch_command(cmd, threaded=True):
    text  = cmd.strip()
    if not text:
        return False

    upper  = text.upper()
    m_x    = re.fullmatch(r"X(-?\d+)", upper)
    m_y    = re.fullmatch(r"Y(-?\d+)", upper)
    m_diag = re.fullmatch(r"M(-?\d+),(-?\d+)", upper)
    m_sq   = re.fullmatch(r"SQ(\d+)", upper)
    m_tr   = re.fullmatch(r"TR(\d+)", upper)
    m_dm   = re.fullmatch(r"DM(\d+)", upper)
    m_zz   = re.fullmatch(r"ZZ(\d+)", upper)
    m_rc1  = re.fullmatch(r"RC(\d+)", upper)
    m_rc   = re.fullmatch(r"RC(\d+),(\d+)", upper)
    m_sp   = re.fullmatch(r"SP(\d+),(\d+)", upper)

    def _th(fn, *a, **kw):
        if threaded:
            _run_in_thread(fn, *a, **kw)
            return True
        return fn(*a, **kw)

    if text.startswith("{"):
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as exc:
            log(f"[JSON] Invalid JSON: {exc}", color=ACCENT2)
            return False
        return _th(_run_draw_spec, spec, label="JSON")
    if upper.startswith("DRAWJSON"):
        payload = text[8:].lstrip(" :")
        try:
            spec = json.loads(payload)
        except json.JSONDecodeError as exc:
            log(f"[JSON] Invalid JSON: {exc}", color=ACCENT2)
            return False
        return _th(_run_draw_spec, spec, label="JSON")
    if upper.startswith("RAW "):
        return _th(_send_passthrough_command, text[4:].strip(), label="RAW")
    if upper in {"PU", "PENUP"}:
        return _th(_set_pen, False)
    if upper in {"PD", "PENDOWN"}:
        return _th(_set_pen, True)
    if upper == "STATUS":
        return _th(_send_passthrough_command, "STATUS", label="STATUS")
    if m_x:
        return _th(_tracked_send, "X", int(m_x.group(1)))
    if m_y:
        return _th(_tracked_send, "Y", int(m_y.group(1)))
    if m_diag:
        return _th(_tracked_diagonal, int(m_diag.group(1)), int(m_diag.group(2)))
    if upper == "HOME":
        return _th(return_to_zero, "HOME")
    if upper == "ZERO":
        return set_zero()
    if m_sq:
        return _th(_run_draw_spec, _make_builtin_shape_spec("SQ", size=int(m_sq.group(1))), label="SHAPE SQ")
    if m_tr:
        return _th(_run_draw_spec, _make_builtin_shape_spec("TR", size=int(m_tr.group(1))), label="SHAPE TR")
    if m_dm:
        return _th(_run_draw_spec, _make_builtin_shape_spec("DM", size=int(m_dm.group(1))), label="SHAPE DM")
    if m_zz:
        return _th(_run_draw_spec, _make_builtin_shape_spec("ZZ", size=int(m_zz.group(1))), label="SHAPE ZZ")
    if m_rc1:
        return _th(_run_draw_spec, _make_builtin_shape_spec("RC", size=int(m_rc1.group(1))), label="SHAPE RC")
    if m_rc:
        return _th(_run_draw_spec, _make_builtin_shape_spec("RC", size=int(m_rc.group(1)), rect_height=int(m_rc.group(2))), label="SHAPE RC")
    if m_sp:
        return _th(_run_draw_spec, _make_builtin_shape_spec("SP", size=int(m_sp.group(1)), rings=int(m_sp.group(2))), label="SHAPE SP")

    log(f"[CMD] Blocked unrecognized command: {text}", color=ACCENT2)
    log("[CMD] Use RAW <command> only if you intentionally want direct pass-through", color=MUTED)
    return False


def send_raw():
    cmd = raw_entry.get().strip()
    if not cmd:
        return
    raw_entry.delete(0, "end")
    _dispatch_command(cmd, threaded=True)


def draw_json_from_editor():
    global preview_origin_override
    spec_text = json_editor.get("1.0", "end").strip()
    if not spec_text:
        return
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        log(f"[JSON] Invalid JSON: {exc}", color=ACCENT2)
        return
    preview_origin_override = None
    _preview_spec_with_fit(spec, label="JSON", update_editor=False)


# ── set_zero: always off the main thread when connected ──────────────────────
def set_zero():
    global current_x, current_y
    if connected and ser is not None:
        _run_in_thread(_send_passthrough_command, "ZERO", label="ZERO HERE", timeout=10)
    else:
        with state_lock:
            current_x = 0
            current_y = 0
        _persist_state(log_error=False)
        root.after(0, _refresh_pos_label)
        log("[ZERO HERE] Origin set to current position", color=ACCENT3)


# ── set_max_x / set_max_y: read current_x/y without holding state_lock while
#    calling tkinter (which must run on main thread) ─────────────────────────
def set_max_x():
    """Read the tracked position and push it to the bound spinbox — main-thread safe."""
    with state_lock:
        val = abs(current_x)
    # bound_x_var is a tkinter var — must only be written on the main thread
    root.after(0, lambda: [bound_x_var.set(str(val)), _persist_state(log_error=False)])


def set_max_y():
    with state_lock:
        val = abs(current_y)
    root.after(0, lambda: [bound_y_var.set(str(val)), _persist_state(log_error=False)])


def _enter_calibration_pose(down):
    if not connected or ser is None:
        log("[PCAL] Connect first to calibrate the servo", color=ACCENT2)
        return
    _run_in_thread(_set_pen, down, label="PCAL")


def _nudge_servo(amount):
    if not _calib_active:
        return
    _pcal_send(f"PCAL N{amount}")


def _store_current_servo_pose(target_down):
    """Query firmware for the real current angle, then store it as UP or DOWN."""
    if not connected or ser is None:
        log("[PCAL] Not connected", color=ACCENT2)
        return

    def _do():
        # Query actual hardware angles
        _clear_response_queue()
        with ser_lock:
            ser.write(b"PCAL?\n")
        log("  >> PCAL?", color=ACCENT)
        deadline = time.time() + 5
        hw_up = hw_dn = None
        while time.time() < deadline:
            try:
                line = response_queue.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line.startswith("PEN_UP="):
                parts = dict(p.split("=") for p in line.split() if "=" in p)
                hw_up = _safe_int(parts.get("PEN_UP"), current_servo_angle)
                hw_dn = _safe_int(parts.get("PEN_DOWN"), current_servo_angle)
            parsed = _parse_ack_line(line)
            if parsed is not None:
                _set_local_state_from_physical(*parsed)
                break

        # Use current pose angle: if pen is down use PEN_DOWN, else PEN_UP
        if pen_is_down:
            angle = hw_dn if hw_dn is not None else current_servo_angle
        else:
            angle = hw_up if hw_up is not None else current_servo_angle

        if target_down:
            _update_pen_calibration_vars(down=angle, active_angle=angle)
            ack = _send_command_wait(f"PCAL D{angle}")
            if ack:
                _set_local_state_from_physical(*ack)
            log(f"[PCAL] Stored {angle}° as pen DOWN", color=ACCENT3)
        else:
            _update_pen_calibration_vars(up=angle, active_angle=angle)
            ack = _send_command_wait(f"PCAL U{angle}")
            if ack:
                _set_local_state_from_physical(*ack)
            log(f"[PCAL] Stored {angle}° as pen UP", color=ACCENT3)

    _run_in_thread(_do)


def _preview_origin_bounds(spec):
    try:
        raw_paths = _coerce_paths(spec)
    except ValueError:
        return None
    _, lim_x, lim_y = _get_limits(force_enforce=True)
    area    = spec.get("area") or {}
    area_x  = max(0, _safe_int(area.get("x"), 0, 0))
    area_y  = max(0, _safe_int(area.get("y"), 0, 0))
    area_w  = max(1, _safe_int(area.get("width"),  lim_x - area_x, 1))
    area_h  = max(1, _safe_int(area.get("height"), lim_y - area_y, 1))
    area_right  = min(lim_x, area_x + area_w)
    area_bottom = min(lim_y, area_y + area_h)
    margin = _safe_int(spec.get("margin"), DEFAULT_DRAW_MARGIN, 0)
    x_min  = max(0, area_x + margin)
    y_min  = max(0, area_y + margin)
    x_max  = min(lim_x, area_right  - margin)
    y_max  = min(lim_y, area_bottom - margin)
    if x_max <= x_min or y_max <= y_min:
        return None

    xs = [x for path in raw_paths for x, _ in path["points"]]
    ys = [y for path in raw_paths for _, y in path["points"]]
    src_min_x, src_max_x = min(xs), max(xs)
    src_min_y, src_max_y = min(ys), max(ys)
    src_w = src_max_x - src_min_x
    src_h = src_max_y - src_min_y
    dst_w = x_max - x_min
    dst_h = y_max - y_min

    fit         = str(spec.get("fit", "auto")).lower()
    keep_aspect = bool(spec.get("keep_aspect", True))
    max_scale_x = (dst_w / src_w) if src_w else 1.0
    max_scale_y = (dst_h / src_h) if src_h else 1.0

    if fit == "fill" and not keep_aspect:
        scale_x = max_scale_x if src_w else 1.0
        scale_y = max_scale_y if src_h else 1.0
    elif not keep_aspect:
        scale_x = min(1.0, max_scale_x) if fit == "auto" and src_w else max_scale_x
        scale_y = min(1.0, max_scale_y) if fit == "auto" and src_h else max_scale_y
    else:
        candidates = []
        if src_w:
            candidates.append(max_scale_x)
        if src_h:
            candidates.append(max_scale_y)
        scale = min(candidates) if candidates else 1.0
        if fit == "auto":
            scale = min(1.0, scale)
        scale_x = scale_y = scale

    scaled_min_x = src_min_x * scale_x
    scaled_max_x = src_max_x * scale_x
    scaled_min_y = src_min_y * scale_y
    scaled_max_y = src_max_y * scale_y
    return {
        "min_x": x_min - scaled_min_x,
        "max_x": x_max - scaled_max_x,
        "min_y": y_min - scaled_min_y,
        "max_y": y_max - scaled_max_y,
        "lim_x": lim_x,
        "lim_y": lim_y,
    }


def _redraw_preview_canvas():
    if "preview_canvas" not in globals():
        return
    preview_canvas.delete("all")
    width = max(1, preview_canvas.winfo_width())
    height = max(1, preview_canvas.winfo_height())
    preview_canvas.create_rectangle(10, 10, width - 10, height - 10, outline=BORDER, width=2)

    spec = preview_job.get("spec")
    planned = preview_job.get("planned") or []
    if not spec or not planned:
        preview_canvas.create_text(width / 2, height / 2, text="Preview a shape or JSON plan",
                                   fill=MUTED, font=FONT_MONO)
        return

    _, lim_x, lim_y = _get_limits(force_enforce=True)
    pad = 20
    usable_w = max(1, width - pad * 2)
    usable_h = max(1, height - pad * 2)
    scale = min(usable_w / max(1, lim_x), usable_h / max(1, lim_y))

    def to_canvas(pt):
        x, y = pt
        return pad + x * scale, height - pad - y * scale

    preview_canvas.create_rectangle(
        pad, height - pad - lim_y * scale, pad + lim_x * scale, height - pad,
        outline=ACCENT3, width=1
    )
    done_left = preview_job.get("segments_done", 0)
    for path in planned:
        points = path["points"]
        draw_path = bool(path.get("draw", True))
        if len(points) < 2:
            continue
        coords = []
        for point in points:
            coords.extend(to_canvas(point))
        preview_canvas.create_line(
            *coords,
            fill=(MUTED if not draw_path else BORDER),
            width=2,
            dash=((4, 4) if not draw_path else ()),
        )

        if draw_path:
            drawn_here = min(done_left, len(points) - 1)
            for idx in range(len(points) - 1):
                color = ACCENT2 if idx < drawn_here else ACCENT
                x1, y1 = to_canvas(points[idx])
                x2, y2 = to_canvas(points[idx + 1])
                preview_canvas.create_line(x1, y1, x2, y2, fill=color, width=3)
            done_left = max(0, done_left - (len(points) - 1))

    cur_x, cur_y = _current_position()
    cx, cy = to_canvas((cur_x, cur_y))
    preview_canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=ACCENT3, outline="")
    preview_canvas.create_text(cx + 28, cy - 10, text="head", fill=ACCENT3, font=FONT_SMALL)

    if preview_origin_override is not None:
        ox, oy = to_canvas(preview_origin_override)
        preview_canvas.create_line(ox - 8, oy, ox + 8, oy, fill=ACCENT2, width=2)
        preview_canvas.create_line(ox, oy - 8, ox, oy + 8, fill=ACCENT2, width=2)


def _preview_drag_move(event):
    global preview_origin_override
    if draw_in_progress:
        return
    spec = preview_job.get("spec")
    if not spec:
        return
    bounds = _preview_origin_bounds(spec)
    if not bounds:
        return
    width = max(1, preview_canvas.winfo_width())
    height = max(1, preview_canvas.winfo_height())
    pad = 20
    usable_w = max(1, width - pad * 2)
    usable_h = max(1, height - pad * 2)
    scale = min(usable_w / max(1, bounds["lim_x"]), usable_h / max(1, bounds["lim_y"]))
    logical_x = (event.x - pad) / scale
    logical_y = (height - pad - event.y) / scale
    preview_origin_override = (
        min(max(logical_x, bounds["min_x"]), bounds["max_x"]),
        min(max(logical_y, bounds["min_y"]), bounds["max_y"]),
    )
    _preview_spec_with_fit(spec, label=preview_job.get("label") or "DRAW", update_editor=True, quiet=True)


# ── toggle_calibrate: pure UI + state, no serial ────────────────────────────
_calib_active = False

def toggle_calibrate():
    global _calib_active, calibration_ready
    _calib_active = not _calib_active
    root.after(0, _apply_calibration_ui_state)
    if _calib_active:
        root.after(0, lambda: enforce_bounds_var.set(False))
        _enter_calibration_pose(False)
        log("[CALIB] Jog to max corner → SET X / SET Y → SAVE CALIB", color=ACCENT3)
    else:
        root.after(0, lambda: enforce_bounds_var.set(True))
        calibration_ready = True
        _persist_state(log_error=True)
        log(f"[CALIB] Limits saved — Max X:{bound_x_var.get()}  Max Y:{bound_y_var.get()}", color=ACCENT3)


# ════════════════════════════════════════════════════════════════════════════
#  DEMO SEQUENCES
# ════════════════════════════════════════════════════════════════════════════
DEMOS = {
    "Calibrate":   [("X",50),("X",-50),("Y",50),("Y",-50)],
    "Shapes tour": [("R","SQ30"),("R","TR30"),("R","DM30"),("R","RC30")],
    "Spiral demo": [("R","SP8,4")],
    "Figure-8":    [("D",30,30),("D",30,-30),("D",-30,-30),("D",-30,30),
                    ("D",-30,30),("D",-30,-30),("D",30,-30),("D",30,30)],
    "Grid lines":  [("X",60),("Y",10),("X",-60),("Y",10),
                    ("X",60),("Y",10),("X",-60),("Y",10),
                    ("X",60),("Y",10),("X",-60),("Y",10)],
    "Star":        [("D",20,50),("D",-40,0),("D",30,-40),
                    ("D",0,50), ("D",-30,-40),("D",40,0)],
    "Return home": [],
}


def run_demo(name):
    seq = DEMOS.get(name, [])
    log(f"\n[DEMO] {name}", color=ACCENT3)

    def runner():
        with motion_lock:
            if not return_to_zero(label="DEMO"):
                return
            if name == "Return home":
                log("[DEMO] Done\n", color=ACCENT3)
                return
            if not _set_pen(True, label="DEMO"):
                return
            for step in seq:
                kind = step[0]
                if   kind == "X": _tracked_send("X", step[1])
                elif kind == "Y": _tracked_send("Y", step[1])
                elif kind == "D": _tracked_diagonal(step[1], step[2])
                elif kind == "R": _dispatch_command(step[1], threaded=False)
                time.sleep(0.2)
            _set_pen(False, label="DEMO")
            return_to_zero(label="DEMO")
            log(f"[DEMO] {name} complete\n", color=ACCENT3)

    _run_in_thread(runner)


# ════════════════════════════════════════════════════════════════════════════
#  SERIAL CONNECTION
# ════════════════════════════════════════════════════════════════════════════
def refresh_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    port_combo["values"] = ports
    if ports:
        port_combo.set(ports[0])


def _sync_with_controller():
    # 1. Read back confirmed position first (no actuation)
    ack = _send_command_wait("STATUS", timeout=8)
    if ack:
        _set_local_state_from_physical(*ack)
        with state_lock:
            log(f"[SYNC] Hardware pos: X:{current_x} Y:{current_y} Pen:{'DOWN' if pen_is_down else 'UP'}", color=ACCENT3)

    # 2. Push speed (safe — no servo movement)
    speed = max(1, _safe_int(speed_var.get(), 30, 1))
    up_angle = _safe_int(pcal_up_var.get(), 90)
    dn_angle = _safe_int(pcal_dn_var.get(), 30)
    log(f"[SYNC] Config: speed={speed}, UP={up_angle}°, DN={dn_angle}°", color=ACCENT)
    _send_command_wait(f"S{speed}")

    # 3. Push pen angle VALUES without triggering any servo actuation.
    #    PCAL D only actuates when pen is currently down.
    #    PCAL U only actuates when pen is currently up.
    #    On fresh boot firmware has penIsDown=false, so PCAL D is safe.
    #    But PCAL U would actuate the servo — we skip it if pen is already up
    #    at the target angle (which is the normal boot state).
    _send_command_wait(f"PCAL D{dn_angle}")   # pen is up on boot → no actuation
    # Only push PCAL U if we actually need to change from firmware defaults,
    # AND the servo won't snap (pen must be down for U to be safe, or we accept
    # the gentle ramp). Skip if pen is up to avoid any movement on connect.
    if pen_is_down:
        _send_command_wait(f"PCAL U{up_angle}")  # pen is down → no actuation
    return True


def toggle_connect():
    global ser, connected
    if connected:
        # Disconnect is instant — safe to do on main thread
        try:
            ser.close()
        except Exception:
            pass
        ser = None
        connected = False
        conn_btn.config(text="CONNECT", bg=ACCENT, fg=BTN_TEXT)
        status_dot.config(fg=ACCENT2)
        status_lbl.config(text="Disconnected")
        log("[DISCONNECTED]", color=ACCENT2)
        return

    port = port_combo.get()
    baud = int(baud_combo.get())

    # Disable the button immediately so it can't be double-clicked
    conn_btn.config(state="disabled", text="CONNECTING…", bg=BORDER, fg=MUTED)

    def _do_connect():
        global ser, connected
        try:
            s = serial.Serial(port, baud, timeout=1)
            time.sleep(2)           # wait for Arduino reset — off-thread, won't freeze UI
        except Exception as e:
            def _fail():
                conn_btn.config(state="normal", text="CONNECT", bg=ACCENT, fg=BTN_TEXT)
                messagebox.showerror("Connection Failed", str(e))
            root.after(0, _fail)
            return

        ser       = s
        connected = True

        def _success():
            conn_btn.config(state="normal", text="DISCONNECT", bg=ACCENT2, fg=BTN_TEXT)
            status_dot.config(fg=ACCENT)
            status_lbl.config(text=f"Connected  {port}")
            log(f"[CONNECTED] {port} @ {baud} baud", color=ACCENT)

        root.after(0, _success)

        # Start reader and sync — still on the background thread
        threading.Thread(target=_read_serial, daemon=True).start()
        time.sleep(0.2)
        _sync_with_controller()

    _run_in_thread(_do_connect)


def _read_serial():
    global ser, connected
    while connected and ser:
        try:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                log(f"  << {line}", color=ACCENT3)
                # Queue all substantive lines so _send_command_wait can see them
                # (OK acks, ERR, PCAL info, SPEED=, NUDGE=, etc.)
                response_queue.put(line)
        except Exception:
            break
    # Reader thread exiting — mark disconnected so _send_command_wait won't hang
    if connected:
        log("[SERIAL] Reader thread exited — connection may be lost", color=ACCENT2)


# ════════════════════════════════════════════════════════════════════════════
#  BUILD GUI
# ════════════════════════════════════════════════════════════════════════════
root = tk.Tk()
root.title("CNC Controller  //  Arduino")
root.configure(bg=BG)
root.geometry("1020x800")
root.minsize(900, 700)
root.resizable(True, True)

# ── ttk dark-theme styling (critical for Linux/Ubuntu) ────────────────────
_style = ttk.Style()
_style.theme_use("clam")
_style.configure("TCombobox",
                  fieldbackground=PANEL, background=BORDER,
                  foreground=TEXT, arrowcolor=ACCENT,
                  selectbackground=CARD, selectforeground=TEXT,
                  bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
_style.map("TCombobox",
           fieldbackground=[("readonly", PANEL), ("disabled", CARD)],
           foreground=[("readonly", TEXT), ("disabled", MUTED)],
           background=[("active", BORDER), ("pressed", CARD)])
_style.configure("Vertical.TScrollbar",
                  background=BORDER, troughcolor=PANEL,
                  arrowcolor=ACCENT, bordercolor=BORDER,
                  lightcolor=BORDER, darkcolor=BORDER)
_style.map("Vertical.TScrollbar",
           background=[("active", ACCENT), ("pressed", ACCENT)])

# Header
hdr = tk.Frame(root, bg=BG); hdr.pack(fill="x", padx=20, pady=(16,4))
tk.Label(hdr, text="CNC CONTROLLER", font=FONT_TITLE, bg=BG, fg=ACCENT).pack(side="left")
tk.Label(hdr, text="//  ARDUINO + TKINTER", font=("Courier New",11), bg=BG, fg=MUTED).pack(side="left", padx=12)

# Connection bar
conn_frame = tk.Frame(root, bg=PANEL, pady=8); conn_frame.pack(fill="x", padx=20, pady=4)
tk.Label(conn_frame, text="PORT",  font=FONT_SMALL, bg=PANEL, fg=MUTED).pack(side="left", padx=(12,4))
port_combo = ttk.Combobox(conn_frame, width=14, font=FONT_MONO); port_combo.pack(side="left", padx=4)
tk.Label(conn_frame, text="BAUD",  font=FONT_SMALL, bg=PANEL, fg=MUTED).pack(side="left", padx=(12,4))
baud_combo = ttk.Combobox(conn_frame, values=["9600","115200","57600"], width=9, font=FONT_MONO)
baud_combo.set("9600"); baud_combo.pack(side="left", padx=4)
tk.Button(conn_frame, text="REFRESH", font=FONT_SMALL, bg=CARD, fg=ACCENT,
          relief="flat", padx=10, command=refresh_ports).pack(side="left", padx=8)
conn_btn = tk.Button(conn_frame, text="CONNECT", font=FONT_LABEL, bg=ACCENT,
                     fg=BTN_TEXT, relief="flat", padx=16, command=toggle_connect)
conn_btn.pack(side="left", padx=4)
status_dot = tk.Label(conn_frame, text="●", font=("Courier New",14), bg=PANEL, fg=ACCENT2)
status_dot.pack(side="left", padx=(16,4))
status_lbl = tk.Label(conn_frame, text="Disconnected", font=FONT_SMALL, bg=PANEL, fg=MUTED)
status_lbl.pack(side="left")

# Emergency STOP button in header area
tk.Button(conn_frame, text="STOP!", font=("Courier New", 12, "bold"),
          bg=ACCENT2, fg=TEXT, relief="flat", padx=20, pady=4,
          command=emergency_stop).pack(side="right", padx=12)

# 3-column main layout — col1 is scrollable, col2 fixed, col3 expands
main = tk.Frame(root, bg=BG); main.pack(fill="both", expand=True, padx=20, pady=8)

_col1_outer = tk.Frame(main, bg=BG)
_col1_outer.pack(side="left", fill="y", padx=(0, 8))
_col1_vsb = tk.Scrollbar(_col1_outer, orient="vertical", width=12,
                          bg=BORDER, troughcolor=PANEL, activebackground=ACCENT)
_col1_vsb.pack(side="right", fill="y")
_col1_canvas = tk.Canvas(_col1_outer, bg=BG, highlightthickness=0,
                          yscrollcommand=_col1_vsb.set)
_col1_canvas.pack(side="left", fill="both", expand=True)
_col1_vsb.config(command=_col1_canvas.yview)
col1 = tk.Frame(_col1_canvas, bg=BG)
_col1_win = _col1_canvas.create_window((0, 0), window=col1, anchor="nw")

def _col1_frame_configure(e):
    _col1_canvas.configure(scrollregion=_col1_canvas.bbox("all"))
    # keep canvas width = inner frame's natural width
    _col1_canvas.configure(width=col1.winfo_reqwidth())
col1.bind("<Configure>", _col1_frame_configure)

def _scroll_widget(widget, event):
    delta = 0
    if getattr(event, "num", None) == 4:
        delta = -1
    elif getattr(event, "num", None) == 5:
        delta = 1
    elif getattr(event, "delta", 0):
        delta = int(-1 * (event.delta / 120))
    if delta:
        widget.yview_scroll(delta, "units")
    return "break"


def _bind_mousewheel(widget, target=None):
    target = target or widget
    # For text/scrolledtext widgets, let their native scroll handler work.
    # Only override with custom handler for the canvas-redirect case.
    if target == widget and isinstance(widget, (tk.Text, scrolledtext.ScrolledText)):
        return  # native scroll already works
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(seq, lambda e, w=target: _scroll_widget(w, e), add="+")


_bind_mousewheel(_col1_canvas, _col1_canvas)
_bind_mousewheel(col1, _col1_canvas)

col2 = tk.Frame(main, bg=BG); col2.pack(side="left", fill="both", padx=8)
col3 = tk.Frame(main, bg=BG); col3.pack(side="left", fill="both", expand=True, padx=(8,0))


def _recursive_bind_mousewheel(widget, target):
    """Bind mousewheel on widget AND every descendant so scroll works everywhere."""
    _bind_mousewheel(widget, target)
    widget.bind("<Enter>", lambda e, w=widget, t=target: _bind_mousewheel(w, t))

def _recursive_bind_all_col1():
    """Walk all col1 descendants and bind mousewheel to col1 canvas scroll."""
    def _walk(w):
        _bind_mousewheel(w, _col1_canvas)
        for child in w.winfo_children():
            _walk(child)
    _walk(col1)

# After building col1 content, bind scroll to all children
root.after(200, _recursive_bind_all_col1)

def make_card(parent, title):
    f = tk.Frame(parent, bg=CARD, pady=4); f.pack(fill="x", pady=4)
    tk.Label(f, text=title, font=FONT_LABEL, bg=CARD, fg=ACCENT).pack(anchor="w", padx=10, pady=(6,2))
    tk.Frame(f, height=1, bg=BORDER).pack(fill="x", padx=10, pady=2)
    return f

def make_btn(parent, text, cmd, color=ACCENT, fg=BTN_TEXT, w=8, **kw):
    cfg = dict(font=FONT_LABEL, bg=color, fg=fg, relief="flat", width=w, padx=4, pady=4, command=cmd)
    cfg.update(kw); return tk.Button(parent, text=text, **cfg)

# ── COL 1: Movement ───────────────────────────────────────────────────────────
mv_card = make_card(col1, "MOVEMENT")

sv_row = tk.Frame(mv_card, bg=CARD); sv_row.pack(padx=10, pady=4)
tk.Label(sv_row, text="Steps:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
step_var = tk.StringVar(value="20")
tk.Spinbox(sv_row, from_=1, to=500, textvariable=step_var, width=6,
           font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
           relief="flat").pack(side="left", padx=6)

def _update_speed(*args):
    s = max(1, _safe_int(speed_var.get(), 30, 1))
    _run_in_thread(_send_passthrough_command, f"S{s}", label="SPEED")

speed_row = tk.Frame(mv_card, bg=CARD); speed_row.pack(padx=10, pady=4, fill="x")
tk.Label(speed_row, text="Speed:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
speed_var = tk.StringVar(value="30")
speed_var.trace_add("write", _update_speed)
tk.Scale(speed_row, from_=1, to=100, variable=speed_var, orient="horizontal",
         bg=CARD, fg=TEXT, highlightthickness=0, troughcolor=PANEL,
         activebackground=ACCENT, showvalue=False).pack(side="left", fill="x", expand=True, padx=6)
tk.Label(speed_row, textvariable=speed_var, font=FONT_MONO, bg=CARD, fg=ACCENT, width=3).pack(side="left")

dpad = tk.Frame(mv_card, bg=CARD); dpad.pack(padx=10, pady=6)
_b   = dict(font=FONT_BIG, width=3, pady=4, repeatdelay=300, repeatinterval=150)
make_btn(dpad,"↖",lambda:move_diagonal(-1,-1), CARD, MUTED,   **_b).grid(row=0,column=0,padx=2,pady=2)
make_btn(dpad,"↑", lambda:move("Y",-1),        ACCENT,BTN_TEXT,**_b).grid(row=0,column=1,padx=2,pady=2)
make_btn(dpad,"↗",lambda:move_diagonal(1,-1),  CARD, MUTED,   **_b).grid(row=0,column=2,padx=2,pady=2)
make_btn(dpad,"←",lambda:move("X",-1),         ACCENT,BTN_TEXT,**_b).grid(row=1,column=0,padx=2,pady=2)
make_btn(dpad,"⊙", lambda: _run_in_thread(return_to_zero, "HOME"),
         ACCENT3,BTN_TEXT,font=FONT_BIG,width=3,pady=4).grid(row=1,column=1,padx=2,pady=2)
make_btn(dpad,"→",lambda:move("X", 1),         ACCENT,BTN_TEXT,**_b).grid(row=1,column=2,padx=2,pady=2)
make_btn(dpad,"↙",lambda:move_diagonal(-1,1),  CARD, MUTED,   **_b).grid(row=2,column=0,padx=2,pady=2)
make_btn(dpad,"↓", lambda:move("Y", 1),        ACCENT,BTN_TEXT,**_b).grid(row=2,column=1,padx=2,pady=2)
make_btn(dpad,"↘",lambda:move_diagonal(1,1),   CARD, MUTED,   **_b).grid(row=2,column=2,padx=2,pady=2)

preset_row = tk.Frame(mv_card, bg=CARD); preset_row.pack(pady=(4,4), padx=10)
tk.Label(preset_row, text="Quick:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
for v in [5,10,20,50,100]:
    tk.Button(preset_row, text=str(v), font=FONT_SMALL, bg=BORDER, fg=TEXT,
              relief="flat", padx=6, pady=2,
              command=lambda v=str(v): step_var.set(v)).pack(side="left", padx=2)

opt_row = tk.Frame(mv_card, bg=CARD); opt_row.pack(padx=10, pady=(0,4), fill="x")
swap_xy_var = tk.BooleanVar(value=False)
inv_x_var   = tk.BooleanVar(value=False)
inv_y_var   = tk.BooleanVar(value=False)
for text, var in [("Swap X/Y",swap_xy_var),("Inv X",inv_x_var),("Inv Y",inv_y_var)]:
    tk.Checkbutton(opt_row, text=text, font=FONT_SMALL, variable=var, bg=CARD, fg=TEXT,
                   selectcolor=PANEL, activebackground=CARD,
                   activeforeground=TEXT).pack(side="left", padx=2)

bnd_row = tk.Frame(mv_card, bg=CARD); bnd_row.pack(padx=10, pady=(0,2), fill="x")
enforce_bounds_var = tk.BooleanVar(value=False)
tk.Checkbutton(bnd_row, text="Enforce limits", font=FONT_SMALL, variable=enforce_bounds_var,
               bg=CARD, fg=TEXT, selectcolor=PANEL,
               activebackground=CARD, activeforeground=TEXT).pack(side="left")
bound_x_var = tk.StringVar(value="500")
bound_y_var = tk.StringVar(value="500")
bounds_set_row = tk.Frame(mv_card, bg=CARD)
for lbl, var, fn in [("Max X:", bound_x_var, set_max_x), ("Max Y:", bound_y_var, set_max_y)]:
    tk.Label(bounds_set_row, text=lbl, font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left", padx=(0,0))
    tk.Spinbox(bounds_set_row, from_=10, to=9999, textvariable=var, width=5,
               font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
               relief="flat").pack(side="left", padx=1)
    tk.Button(bounds_set_row, text="SET", font=("Courier New", 7), bg=BORDER, fg=TEXT,
              relief="flat", padx=3, pady=1, command=fn).pack(side="left", padx=(0, 8))

pos_row = tk.Frame(mv_card, bg=CARD); pos_row.pack(padx=10, pady=(0,2), fill="x")
pos_lbl = tk.Label(pos_row, text="X: 0/0  Y: 0/0", font=FONT_MONO, bg=CARD, fg=ACCENT)
pos_lbl.pack(side="left")
pen_lbl = tk.Label(pos_row, text="Pen: UP  Servo: 90°", font=FONT_MONO, bg=CARD, fg=ACCENT3)
pen_lbl.pack(side="left", padx=(12, 0))

pos_btn_row = tk.Frame(mv_card, bg=CARD); pos_btn_row.pack(padx=10, pady=(0,6), fill="x")
calib_btn = tk.Button(pos_btn_row, text="CALIBRATE", font=FONT_SMALL, bg=BORDER, fg=TEXT,
                      relief="flat", padx=6, pady=2, command=toggle_calibrate)
calib_btn.pack(side="left", padx=(0, 6))
tk.Button(pos_btn_row, text="ZERO HERE", font=FONT_SMALL, bg=BORDER, fg=TEXT,
          relief="flat", padx=6, pady=2, command=set_zero).pack(side="left")

pen_action_row = tk.Frame(mv_card, bg=CARD)
# pen_action_row is shown in normal mode; starts hidden (shown after _refresh_control_visibility)
make_btn(pen_action_row, "MOVE UP",
         lambda: move_pen(False),
         ACCENT3, BTN_TEXT, 10).pack(side="left", padx=(0, 6))
make_btn(pen_action_row, "MOVE DOWN",
         lambda: move_pen(True),
         ACCENT2, BTN_TEXT, 12).pack(side="left")

calib_action_row = tk.Frame(mv_card, bg=CARD)
# calib_action_row is shown only in calibrate mode
make_btn(calib_action_row, "SET UP",
         lambda: _store_current_servo_pose(False), ACCENT3, BTN_TEXT, 10).pack(side="left", padx=(0, 6))
make_btn(calib_action_row, "SET DOWN",
         lambda: _store_current_servo_pose(True),  ACCENT2, BTN_TEXT, 12).pack(side="left")

pcal_card = make_card(col1, "PEN SETUP")

def _pcal_query():
    def _do():
        if not connected or ser is None:
            log("[PCAL] Not connected", color=ACCENT2); return
        _clear_response_queue()
        with ser_lock:
            ser.write(b"PCAL?\n")
        log("  >> PCAL?", color=ACCENT)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                line = response_queue.get(timeout=max(0.05, deadline - time.time()))
                log(f"  << {line}", color=ACCENT3)
                if line.startswith("PEN_UP="):
                    parts = dict(p.split("=") for p in line.split() if "=" in p)
                    _update_pen_calibration_vars(
                        up=parts.get("PEN_UP", pcal_up_var.get()),
                        down=parts.get("PEN_DOWN", pcal_dn_var.get()),
                    )
                if line.startswith("OK "):
                    break
            except queue.Empty:
                break
    _run_in_thread(_do)

def _pcal_send(raw_cmd):
    """Send a PCAL command; read firmware's angle-report lines before the OK ack."""
    def _do():
        if not connected or ser is None:
            log("[PCAL] Not connected", color=ACCENT2)
            return
        _clear_response_queue()
        with ser_lock:
            ser.write((raw_cmd.strip() + "\n").encode())
        log(f"  >> {raw_cmd}", color=ACCENT)

        deadline   = time.time() + 10   # servo ramp can take ~1 s
        hw_up = hw_dn = hw_nudge = None
        ack = None
        while time.time() < deadline:
            try:
                line = response_queue.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line.startswith("PEN_UP="):
                parts = dict(p.split("=") for p in line.split() if "=" in p)
                hw_up  = _safe_int(parts.get("PEN_UP"),   None)
                hw_dn  = _safe_int(parts.get("PEN_DOWN"), None)
            elif line.startswith("NUDGE="):
                parts = dict(p.split("=") for p in line.split() if "=" in p)
                hw_nudge = _safe_int(parts.get("NUDGE"),    None)
                hw_up    = _safe_int(parts.get("PEN_UP"),   hw_up)
                hw_dn    = _safe_int(parts.get("PEN_DOWN"), hw_dn)
            parsed = _parse_ack_line(line)
            if parsed is not None:
                ack = parsed
                break
            if line.startswith("ERR "):
                log(f"[PCAL] Error: {line}", color=ACCENT2)
                return

        if ack is None:
            log(f"[PCAL] No response for {raw_cmd}", color=ACCENT2)
            return

        _set_local_state_from_physical(*ack)

        # Update Python-side vars from what firmware actually reported
        if hw_nudge is not None:
            if pen_is_down:
                _update_pen_calibration_vars(down=hw_nudge, active_angle=hw_nudge)
            else:
                _update_pen_calibration_vars(up=hw_nudge, active_angle=hw_nudge)
        elif hw_up is not None or hw_dn is not None:
            active = hw_up if not pen_is_down else hw_dn
            _update_pen_calibration_vars(up=hw_up, down=hw_dn, active_angle=active)
        else:
            # Fallback: derive from raw_cmd
            m_u = re.search(r"U(\d+)", raw_cmd)
            m_d = re.search(r"D(\d+)", raw_cmd)
            if m_u:
                _update_pen_calibration_vars(up=m_u.group(1), active_angle=m_u.group(1))
            elif m_d:
                _update_pen_calibration_vars(down=m_d.group(1), active_angle=m_d.group(1))

        log(f"[PCAL] {raw_cmd} → OK", color=ACCENT3)

    _run_in_thread(_do)

pcal_modes_row = tk.Frame(pcal_card, bg=CARD); pcal_modes_row.pack(fill="x", padx=10, pady=(4,2))
make_btn(pcal_modes_row, "POSE UP", lambda: _enter_calibration_pose(False), ACCENT3, BTN_TEXT, 9).pack(side="left", padx=(0, 6))
make_btn(pcal_modes_row, "POSE DOWN", lambda: _enter_calibration_pose(True), ACCENT2, BTN_TEXT, 11).pack(side="left")

pcal_angles_row = tk.Frame(pcal_card, bg=CARD); pcal_angles_row.pack(fill="x", padx=10, pady=(4,2))
tk.Label(pcal_angles_row, text="UP °:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
pcal_up_var = tk.StringVar(value="90")
tk.Spinbox(pcal_angles_row, from_=0, to=180, textvariable=pcal_up_var, width=5,
           font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
           relief="flat").pack(side="left", padx=4)
tk.Button(pcal_angles_row, text="SET UP", font=FONT_SMALL, bg=ACCENT3, fg=BTN_TEXT,
          relief="flat", padx=6, pady=2,
          command=lambda: _pcal_send(f"PCAL U{pcal_up_var.get()}")).pack(side="left")
tk.Button(pcal_angles_row, text="TEST", font=("Courier New", 7), bg=BORDER, fg=TEXT,
          relief="flat", padx=4, pady=1,
          command=lambda: _pcal_send(f"PCAL U{pcal_up_var.get()}")).pack(side="left", padx=4)

pcal_dn_row = tk.Frame(pcal_card, bg=CARD); pcal_dn_row.pack(fill="x", padx=10, pady=(0,2))
tk.Label(pcal_dn_row, text="DN °:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
pcal_dn_var = tk.StringVar(value="30")
tk.Spinbox(pcal_dn_row, from_=0, to=180, textvariable=pcal_dn_var, width=5,
           font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
           relief="flat").pack(side="left", padx=4)
tk.Button(pcal_dn_row, text="SET DN", font=FONT_SMALL, bg=ACCENT2, fg=BTN_TEXT,
          relief="flat", padx=6, pady=2,
          command=lambda: _pcal_send(f"PCAL D{pcal_dn_var.get()}")).pack(side="left")
tk.Button(pcal_dn_row, text="TEST", font=("Courier New", 7), bg=BORDER, fg=TEXT,
          relief="flat", padx=4, pady=1,
          command=lambda: _pcal_send(f"PCAL D{pcal_dn_var.get()}")).pack(side="left", padx=4)

pcal_nudge_row = tk.Frame(pcal_card, bg=CARD); pcal_nudge_row.pack(fill="x", padx=10, pady=(0,6))
tk.Label(pcal_nudge_row, text="Nudge:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
_pcal_nudge_var = tk.StringVar(value="2")
tk.Spinbox(pcal_nudge_row, from_=1, to=20, textvariable=_pcal_nudge_var, width=4,
           font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
           relief="flat").pack(side="left", padx=4)
tk.Button(pcal_nudge_row, text="▲", font=FONT_LABEL, bg=PANEL, fg=ACCENT,
          relief="flat", width=3, pady=2,
          command=lambda: _pcal_send(f"PCAL N{_pcal_nudge_var.get()}")).pack(side="left", padx=2)
tk.Button(pcal_nudge_row, text="▼", font=FONT_LABEL, bg=PANEL, fg=ACCENT,
          relief="flat", width=3, pady=2,
          command=lambda: _pcal_send(f"PCAL N-{_pcal_nudge_var.get()}")).pack(side="left", padx=2)
tk.Button(pcal_nudge_row, text="SYNC", font=FONT_SMALL, bg=BORDER, fg=TEXT,
          relief="flat", padx=6, pady=2, command=_pcal_query).pack(side="left", padx=(8,0))

def _pcal_swap():
    u, d = pcal_up_var.get(), pcal_dn_var.get()
    pcal_up_var.set(d)
    pcal_dn_var.set(u)
    _pcal_send(f"PCAL U{d}")
    _pcal_send(f"PCAL D{u}")

tk.Button(pcal_nudge_row, text="SWAP", font=FONT_SMALL, bg=BORDER, fg=TEXT,
          relief="flat", padx=6, pady=2, command=_pcal_swap).pack(side="left", padx=4)
pcal_angles_row.pack_forget()
pcal_dn_row.pack_forget()
# (These rows are shown when calibrate mode is active via _refresh_control_visibility)

# ── COL 1: Shapes ─────────────────────────────────────────────────────────────
sh_card = make_card(col1, "SHAPES  (auto-homes first)")
sz_row  = tk.Frame(sh_card, bg=CARD); sz_row.pack(padx=10, pady=4)
tk.Label(sz_row, text="Size:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
size_var = tk.StringVar(value="30")
tk.Spinbox(sz_row, from_=5, to=500, textvariable=size_var, width=6,
           font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
           relief="flat").pack(side="left", padx=6)
tk.Label(sz_row, text="Rings:", font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left")
rings_var = tk.IntVar(value=3)
tk.Spinbox(sz_row, from_=1, to=8, textvariable=rings_var, width=4,
           font=FONT_MONO, bg=PANEL, fg=TEXT, buttonbackground=BORDER,
           relief="flat").pack(side="left", padx=6)

shapes_grid = tk.Frame(sh_card, bg=CARD); shapes_grid.pack(padx=10, pady=6)
for i, (label, prefix) in enumerate([("SQUARE","SQ"),("TRIANGLE","TR"),
                                       ("DIAMOND","DM"),("RECTANGLE","RC"),
                                       ("ZIGZAG","ZZ"),("SPIRAL","SP")]):
    r, c = divmod(i, 2)
    tk.Button(shapes_grid, text=label, font=FONT_SMALL, bg=PANEL, fg=ACCENT,
              relief="flat", width=10, pady=5,
              command=lambda p=prefix: send_shape(p)).grid(row=r, column=c, padx=3, pady=3)

_bind_persisted_vars()

# ── COL 2: Demo programs ──────────────────────────────────────────────────────
demo_card = make_card(col2, "DEMO PROGRAMS  (auto-homes first)")
for name, desc in [
    ("Calibrate",   "Jogs each axis ±50 steps"),
    ("Shapes tour", "Square→Triangle→Diamond→Rect"),
    ("Spiral demo", "Expanding spiral outward"),
    ("Figure-8",    "Diagonal figure-eight"),
    ("Grid lines",  "3 parallel horizontal lines"),
    ("Star",        "6-point star pattern"),
    ("Return home", "Drive motors back to (0,0)"),
]:
    row = tk.Frame(demo_card, bg=CARD); row.pack(fill="x", padx=10, pady=3)
    tk.Button(row, text=name, font=FONT_SMALL, bg=ACCENT, fg=BTN_TEXT,
              relief="flat", width=14, pady=4,
              command=lambda n=name: run_demo(n)).pack(side="left")
    tk.Label(row, text=desc, font=FONT_SMALL, bg=CARD, fg=MUTED,
             wraplength=160, justify="left").pack(side="left", padx=8)

# ── COL 2: Raw command ────────────────────────────────────────────────────────
raw_card  = make_card(col2, "RAW COMMAND")
raw_inner = tk.Frame(raw_card, bg=CARD); raw_inner.pack(fill="x", padx=10, pady=6)
raw_entry = tk.Entry(raw_inner, font=FONT_MONO, bg=PANEL, fg=ACCENT,
                     insertbackground=ACCENT, relief="flat", width=18)
raw_entry.pack(side="left", ipady=4, padx=(0,6))
raw_entry.bind("<Return>", lambda e: send_raw())
tk.Button(raw_inner, text="SEND", font=FONT_LABEL, bg=ACCENT, fg=BTN_TEXT,
          relief="flat", padx=12, pady=4, command=send_raw).pack(side="left")

# ── COL 2: JSON draw API ──────────────────────────────────────────────────────
json_card   = make_card(col2, "JSON DRAW API")
json_btn_row = tk.Frame(json_card, bg=CARD); json_btn_row.pack(fill="x", padx=10, pady=(4,4))
tk.Button(json_btn_row, text="▶ PREVIEW JSON", font=FONT_LABEL, bg=ACCENT3, fg=BTN_TEXT,
          relief="flat", padx=12, pady=4, command=draw_json_from_editor).pack(side="left")

def _load_json_file():
    """Open a file dialog to load a .json drawing file into the editor."""
    fpath = filedialog.askopenfilename(
        title="Load JSON Drawing",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        initialdir=str(Path(__file__).with_name("drawings")),
    )
    if not fpath:
        return
    try:
        text = Path(fpath).read_text(encoding="utf-8")
        # Validate it's valid JSON
        json.loads(text)
        json_editor.delete("1.0", "end")
        json_editor.insert("1.0", text)
        log(f"[JSON] Loaded: {Path(fpath).name}", color=ACCENT3)
    except json.JSONDecodeError as exc:
        messagebox.showerror("Invalid JSON", f"File is not valid JSON:\n{exc}")
    except Exception as exc:
        messagebox.showerror("Load Error", str(exc))

tk.Button(json_btn_row, text="📂 LOAD FILE", font=FONT_LABEL, bg=BORDER, fg=TEXT,
          relief="flat", padx=10, pady=4, command=_load_json_file).pack(side="left", padx=(8, 0))
tk.Label(json_btn_row, text="pen:'up' = travel",
         font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left", padx=10)
json_editor = scrolledtext.ScrolledText(json_card, height=12, font=FONT_SMALL,
                                        bg=PANEL, fg=TEXT, relief="flat",
                                        insertbackground=ACCENT,
                                        selectbackground=ACCENT, selectforeground=BG)
json_editor.pack(fill="x", padx=10, pady=(0,6))
_bind_mousewheel(json_editor, json_editor)
json_editor.insert("1.0", json.dumps({
    "auto_home": True,
    "fit":    "contain",
    "align":  "start",
    "margin": 12,
    "paths":  [
        {"pen": "down", "closed": True, "points": [[0,0],[100,0],[100,60],[0,60]]},
        {"pen": "up",   "points": [[0,0],[100,60]]},
        {"pen": "down", "points": [[100,60],[100,0],[0,60]]}
    ]
}, indent=2))

# (Draw Preview is now in col3 — see below)

# ── Global Keyboard Shortcuts (Linux Tkinter fixes) ─────────────────────────
def _bind_linux_shortcuts(widget):
    """Bind Ctrl+A, Ctrl+C, Ctrl+V for Linux Tkinter text widgets."""
    def select_all(event):
        event.widget.tag_add("sel", "1.0", "end")
        return "break"
    def select_all_entry(event):
        event.widget.select_range(0, "end")
        event.widget.icursor("end")
        return "break"
    if isinstance(widget, (tk.Text, scrolledtext.ScrolledText)):
        widget.bind("<Control-a>", select_all)
        widget.bind("<Control-A>", select_all)
    elif isinstance(widget, tk.Entry):
        widget.bind("<Control-a>", select_all_entry)
        widget.bind("<Control-A>", select_all_entry)

def _recursive_bind_shortcuts(widget):
    _bind_linux_shortcuts(widget)
    for child in widget.winfo_children():
        _recursive_bind_shortcuts(child)

root.bind("<Map>", lambda e: _recursive_bind_shortcuts(root), add="+")

# ── COL 3: Draw Preview (above console) ──────────────────────────────────────
preview_card = make_card(col3, "DRAW PREVIEW  (drag to reposition · confirm to draw)")
preview_status_var = tk.StringVar(value="Load a shape or paste JSON → PREVIEW JSON")
tk.Label(preview_card, textvariable=preview_status_var, font=FONT_SMALL, bg=CARD, fg=MUTED).pack(anchor="w", padx=10, pady=(4, 2))
preview_canvas = tk.Canvas(preview_card, height=280, bg=PANEL, highlightthickness=0)
preview_canvas.pack(fill="x", padx=10, pady=6)
preview_canvas.bind("<B1-Motion>", _preview_drag_move)
preview_canvas.bind("<Configure>", lambda e: _redraw_preview_canvas())

preview_btn_row = tk.Frame(preview_card, bg=CARD); preview_btn_row.pack(fill="x", padx=10, pady=(0, 8))
confirm_draw_btn = tk.Button(preview_btn_row, text="✔ CONFIRM DRAW", font=FONT_LABEL, bg=ACCENT, fg=BTN_TEXT,
                             relief="flat", padx=14, pady=5, command=_commit_preview_draw)
confirm_draw_btn.pack(side="left")
clear_preview_btn = tk.Button(preview_btn_row, text="CLEAR", font=FONT_SMALL, bg=BORDER, fg=TEXT,
                              relief="flat", padx=10, pady=5, command=_clear_preview)
clear_preview_btn.pack(side="left", padx=(8, 0))
tk.Label(preview_btn_row,
         text="Drag canvas to move origin  ·  green=done  teal=pending",
         font=FONT_SMALL, bg=CARD, fg=MUTED, wraplength=240, justify="left").pack(side="left", padx=10)

# ── COL 3: Console ────────────────────────────────────────────────────────────
con_card = make_card(col3, "SERIAL CONSOLE")
console  = scrolledtext.ScrolledText(con_card, font=FONT_MONO, bg="#0a0c12",
                                     fg=TEXT, relief="flat", state="disabled",
                                     height=24)
console.pack(fill="both", expand=True, padx=10, pady=6)
_bind_mousewheel(console, console)
con_btns = tk.Frame(con_card, bg=CARD); con_btns.pack(fill="x", padx=10, pady=(0,8))

def clear_console():
    console.config(state="normal")
    console.delete("1.0", "end")
    console.config(state="disabled")

tk.Button(con_btns, text="CLEAR", font=FONT_SMALL, bg=BORDER, fg=TEXT,
          relief="flat", padx=12, pady=3, command=clear_console).pack(side="left")
tk.Label(con_btns, text="green = sent  |  yellow = received  |  red = error",
         font=FONT_SMALL, bg=CARD, fg=MUTED).pack(side="left", padx=12)

# ── COL 3: Command reference ──────────────────────────────────────────────────
ref_card = make_card(col3, "COMMAND REFERENCE")
ref_text = scrolledtext.ScrolledText(ref_card, height=10, font=FONT_SMALL,
                                     bg=PANEL, fg=MUTED, relief="flat")
ref_text.pack(fill="x", padx=10, pady=6)
_bind_mousewheel(ref_text, ref_text)
ref_text.insert("1.0", "\n".join([
    "X<n>      move X (negative = reverse)",
    "Y<n>      move Y (negative = reverse)",
    "M<x,y>    diagonal move",
    "PU / PD   pen up / pen down",
    "PCAL U<n> set pen-UP angle (0-180)",
    "PCAL D<n> set pen-DOWN angle (0-180)",
    "PCAL N<n> nudge servo ±n degrees",
    "PCAL?     query current pen angles",
    "S<n>      set motor speed (RPM)",
    "STOP      emergency stop / cut power",
    "STATUS    read controller position",
    "ZERO      mark current spot as origin",
    "SQ<n>     draw square",
    "TR<n>     draw triangle",
    "DM<n>     draw diamond",
    "RC<w,h>   draw rectangle",
    "ZZ<n>     draw zigzag (4 repeats)",
    "SP<n,r>   spiral  n=startSize  r=rings",
    "DRAWJSON { ... }  safe JSON draw plan",
    "RAW <cmd>  explicit pass-through to Arduino",
    "HOME      drive back to (0,0)",
    "",
    "JSON keys: paths/points, area, fit, align, margin, origin",
    "Per-path: use draw:false or pen:'up' for travel moves",
    "fit=auto keeps size unless too big, contain fills area",
    "Pins: Motor X = 5,6,7,8",
    "      Motor Y = 9,10,11,12",
    "      Servo   = 13",
    "Speed: 50 RPM",
    "",
    "ZERO HERE  marks current spot as (0,0)",
    "CALIBRATE  jog to corner, SET X/Y limits",
    "Shapes, demos, and JSON plans stay inside saved bounds",
]))
ref_text.config(state="disabled")

# Status bar
bar = tk.Frame(root, bg=PANEL, pady=4); bar.pack(fill="x", padx=20, pady=(0,8))
tk.Label(bar, text="Motor X: pins 5,6,7,8   |   Motor Y: pins 9,10,11,12   |   Servo: pin 13   |   "
                   "Baud: 9600   |   Steps/rev: 200",
         font=FONT_SMALL, bg=PANEL, fg=MUTED).pack(side="left", padx=12)


def _on_close():
    global ser, connected
    _persist_state(log_error=False)
    if connected and ser is not None:
        try:
            ser.close()
        except Exception:
            pass
        ser = None
        connected = False
    root.destroy()


root.protocol("WM_DELETE_WINDOW", _on_close)
_load_state()
_refresh_control_visibility()
_refresh_pos_label()
_refresh_pen_label()
_refresh_preview_status()
_redraw_preview_canvas()

refresh_ports()
log("CNC Controller ready — fully tracked edition.", color=ACCENT)
log("1. Select port → CONNECT", color=MUTED)
log("2. D-pad to jog.  ⊙ = drive back to (0,0)", color=MUTED)
log("3. ZERO HERE marks your current spot as origin", color=MUTED)
log("4. Shapes, demos, and JSON plans stay inside saved limits\n", color=MUTED)

root.mainloop()
