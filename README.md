# 🖊️ Arduino CNC Pen Plotter — Python Tkinter Controller

> A full-featured desktop controller for a 2-axis stepper motor pen plotter with servo-controlled pen lift. Built with Python + Tkinter on the PC side and Arduino on the hardware side. Supports manual jogging, calibration, built-in shapes, demo programs, and a fully custom JSON drawing API.

---

## 📸 Project Photos

<!-- ADD YOUR PHOTOS HERE -->
> 📷 **Photo 1** — Full machine overview
> <img width="1000" height="800" alt="cnc2" src="https://github.com/user-attachments/assets/72c7cd42-1390-4261-9e06-1960510b366d" />
> <img width="1000" height="800" alt="image" src="https://github.com/user-attachments/assets/5e85b1ef-4ed8-44ef-abc1-cd799dfa5dac" />


> 📷 **Photo 2** — PC software running with a shape loaded  
> <img width="1600" height="800" alt="image" src="https://github.com/user-attachments/assets/2de940f9-99d7-489f-8146-437368cc9ecc" />

## Table of Contents

- [How It Works — Big Picture](#how-it-works--big-picture)
- [System Architecture](#system-architecture)
- [Serial Protocol](#serial-protocol)
- [Threading Model](#threading-model)
- [State Management](#state-management)
- [Coordinate System](#coordinate-system)
- [The JSON Drawing API](#the-json-drawing-api)
- [Drawing Pipeline Step-by-Step](#drawing-pipeline-step-by-step)
- [Pen Calibration System](#pen-calibration-system)
- [GUI Layout](#gui-layout)
- [Hardware Wiring](#hardware-wiring)
- [Installation](#installation)
- [Command Reference](#command-reference)

---

## How It Works — Big Picture

```
┌─────────────────────────────────────────────────────┐
│                   YOUR COMPUTER                      │
│                                                      │
│  ┌─────────────┐    ┌──────────────────────────┐    │
│  │   Tkinter   │    │    Background Threads     │    │
│  │  GUI (Main  │───▶│  motion_lock  ser_lock    │    │
│  │   Thread)   │    │  • _tracked_send()        │    │
│  └──────┬──────┘    │  • _set_pen()             │    │
│         │           │  • _run_draw_spec()        │    │
│         │           └────────────┬──────────────┘    │
│         │                        │                    │
│         ▼                        ▼                    │
│  ┌─────────────┐    ┌───────────────────────────┐    │
│  │  State +    │    │     Serial Write           │    │
│  │  Preview    │    │   cmd + "\n" → Arduino     │    │
│  │  Canvas     │    └───────────┬───────────────┘    │
│  └─────────────┘                │                    │
│                    ┌────────────▼───────────────┐    │
│                    │   _read_serial() thread     │    │
│                    │   Reads "OK X=.. Y=.. PEN"  │    │
│                    │   puts line → response_queue│    │
│                    └────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
              USB / Serial (9600 baud default)
┌─────────────────────────────────────────────────────┐
│                    ARDUINO                           │
│                                                      │
│   Parses text commands  →  drives steppers + servo   │
│   Sends back:  OK X=<n> Y=<n> PEN=UP|DOWN           │
└─────────────────────────────────────────────────────┘
         │              │              │
    Motor X          Motor Y        Servo
  pins 5,6,7,8     pins 9,10,11,12   pin 13
```

The PC always **sends a command → waits for an OK acknowledgement → updates local state**. Nothing is fire-and-forget. This means the PC's tracked position is always in sync with where the hardware actually is.

---

## System Architecture

### Files

```
cnc_controller.py     ← entire application (single file)
control_state.json    ← auto-saved on every setting change
```

### Key Global State Variables

| Variable | Type | Purpose |
|---|---|---|
| `ser` | `serial.Serial` | Active serial port object |
| `connected` | `bool` | Whether a port is open |
| `current_x / current_y` | `int` | Tracked logical position |
| `pen_is_down` | `bool` | Current pen state |
| `calibration_ready` | `bool` | Whether bounds have been saved |
| `draw_in_progress` | `bool` | Blocks manual moves during draw |
| `preview_job` | `dict` | Current loaded preview (spec + planned paths) |
| `preview_origin_override` | `tuple` | Canvas-drag repositioning offset |

---

## Serial Protocol

The Arduino expects **newline-terminated ASCII commands**. The PC sends one command and then blocks on `response_queue` waiting for an acknowledgement.

### Commands sent by PC → Arduino

```
X<n>          Move X axis by n steps  (negative = reverse direction)
Y<n>          Move Y axis by n steps
M<dx>,<dy>    Diagonal move (both axes simultaneously)
PU            Pen Up
PD            Pen Down
S<n>          Set speed to n (RPM)
STATUS        Query current position without moving
ZERO          Mark current hardware position as (0,0)
HOME          Drive back to physical origin (0,0)
STOP          Emergency stop
PCAL U<n>     Set pen-up servo angle  (0–180°)
PCAL D<n>     Set pen-down servo angle (0–180°)
PCAL N<±n>    Nudge servo ±n degrees from current
PCAL?         Query current pen angles from firmware
```

### Acknowledgement format: Arduino → PC

Every command gets exactly **one** response line:

```
OK X=<n> Y=<n> PEN=UP|DOWN
```

or on error:

```
ERR <message>
```

The regex `ACK_RE` parses this:
```python
ACK_RE = re.compile(r"^OK X=(-?\d+) Y=(-?\d+) PEN=(UP|DOWN)$")
```

### Info lines (no acknowledgement expected)

```
PEN_UP=<n> PEN_DOWN=<n>        Firmware reporting calibration angles
NUDGE=<n> PEN_UP=<n> PEN_DOWN=<n>   Servo nudge result
```

These are also routed through `response_queue` and parsed by `_pcal_send()`.

---

## Threading Model

The Tkinter main thread **must never block**. All serial operations happen in daemon threads.

```
Main Thread (Tkinter)
│
├── User clicks button
│        │
│        └──▶ _run_in_thread(fn, *args)
│                   │
│                   └──▶ daemon Thread
│                              │
│                              ├── acquires motion_lock   (prevents overlapping moves)
│                              ├── acquires ser_lock      (protects ser.write)
│                              ├── sends command
│                              ├── blocks on response_queue.get(timeout=...)
│                              ├── parses OK ack
│                              ├── calls _set_local_state_from_physical()
│                              │       │
│                              │       └──▶ root.after(0, _refresh_pos_label)
│                              │           root.after(0, _refresh_pen_label)
│                              │           root.after(0, _redraw_preview_canvas)
│                              └── releases locks
│
└── _read_serial() daemon thread  (started on connect)
         │
         └── loops forever: readline() → log() → response_queue.put(line)
```

**`motion_lock`** is an `RLock` (re-entrant). This means `_run_draw_spec` can hold it for an entire multi-step drawing while still calling `_set_pen` and `_move_to_absolute` internally without deadlocking.

**`ser_lock`** is a plain `Lock`. It only wraps the single `ser.write()` call.

**`state_lock`** protects reads/writes of `current_x`, `current_y`, `pen_is_down`.

---

## State Management

Settings are auto-persisted to `control_state.json` on every change via `tkinter` variable traces.

```python
def _bind_persisted_vars():
    def _save(*_):
        _persist_state(log_error=False)
    for var in [step_var, speed_var, size_var, ...]:
        var.trace_add("write", _save)
```

`_persist_state()` is thread-safe: it snapshots plain Python values (not tkinter vars), then schedules the actual JSON write on the main thread via `root.after(0, _write_on_main)`.

On startup, `_load_state()` restores all settings and the last known position — so if you close the app and reopen it, jogging continues from where you left off.

---

## Coordinate System

The app separates **logical coordinates** (what the GUI and JSON spec use) from **physical coordinates** (what the Arduino sees). Three transformation options are available in the UI:

| Setting | Effect |
|---|---|
| Swap X/Y | Logical X becomes physical Y and vice versa |
| Invert X | Physical X = -Logical X |
| Invert Y | Physical Y = -Logical Y |

```
Logical (GUI) ─── _logical_to_physical() ───▶ Physical (Arduino command)
Physical (Arduino ack) ─── _physical_to_logical() ───▶ Logical (state update)
```

This means you can mount your motors in any orientation and just flip the checkboxes to compensate, without touching any wiring.

**Origin** is always `(0, 0)`. The ZERO HERE button marks wherever the head currently is as `(0, 0)` in both the PC's tracking and the Arduino's internal counter.

---

## The JSON Drawing API

This is the most powerful feature. You describe a drawing as a set of paths in your own coordinate space — the controller scales, fits, and clips them to your calibrated bed automatically.

### Minimal example

```json
{
  "paths": [
    { "points": [[0,0],[100,0],[100,100],[0,100]], "closed": true }
  ]
}
```

### Full schema

```json
{
  "auto_home":    true,          // drive to (0,0) before starting
  "return_home":  false,         // drive back to (0,0) after finishing
  "fit":          "auto",        // "auto" | "fill" | "contain"
  "align":        "start",       // "start" | "center" | "origin"
  "keep_aspect":  true,          // lock aspect ratio when scaling
  "margin":       5,             // safe margin inside bed bounds (steps)
  "segment_delay": 0.05,         // seconds between each segment

  "area": {                      // optional: restrict draw to sub-area
    "x": 0, "y": 0,
    "width": 300, "height": 200
  },

  "origin": { "x": 10, "y": 10 }, // optional: exact placement offset

  "paths": [
    {
      "points": [[x1,y1], [x2,y2], ...],
      "closed": false,           // append first point at end if true
      "pen":    "down",          // "down" (draw) or "up" (travel move)
      "draw":   true             // same as pen:"down", alternative key
    }
  ]
}
```

### Travel moves (pen up paths)

Paths with `"pen": "up"` or `"draw": false` move the head without drawing. Use these to jump between disconnected strokes:

```json
{
  "paths": [
    { "pen": "down", "points": [[0,0],[50,50]] },
    { "pen": "up",   "points": [[50,50],[100,0]] },
    { "pen": "down", "points": [[100,0],[150,50]] }
  ]
}
```

---

## Drawing Pipeline Step-by-Step

```
User pastes JSON in editor
         │
         ▼
  ① _coerce_paths(spec)
     Normalise: single path / multi-path / closed paths
     Validate: at least 2 points, no invalid coords
         │
         ▼
  ② _fit_paths_to_area(paths, spec)
     ┌──────────────────────────────────────────────┐
     │  Read calibrated bed limits (lim_x, lim_y)   │
     │  Subtract margin → safe draw zone             │
     │  Find bounding box of all input paths          │
     │  Compute scale_x, scale_y                     │
     │    fit=auto  → scale ≤ 1.0 (never enlarge)    │
     │    fit=fill  → scale to fill zone              │
     │    keep_aspect=true → use min(scale_x,scale_y) │
     │  Compute translation (tx, ty)                  │
     │    align=start  → first point near zone origin │
     │    align=center → centred in zone              │
     │  Apply scale + translate → integer step coords │
     └──────────────────────────────────────────────┘
         │
         ▼
  ③ preview_job updated → _redraw_preview_canvas()
     Shows teal lines (pending) on canvas
     Dragging canvas updates preview_origin_override
         │
         ▼
  ④ User clicks CONFIRM DRAW
         │
         ▼
  ⑤ _run_draw_spec() (background thread)
     for each path:
       PU  →  move to path[0]  →  PD (if draw path)
       for each subsequent point:
         move_to_absolute(x, y)
         segments_done++
         redraw canvas (red = done segment)
       PU
     optional: HOME
```

The canvas shows progress live: **teal = pending**, **red = completed**.

---

## Pen Calibration System

The servo has two target angles: **UP** (pen not touching paper) and **DOWN** (pen pressing on paper). These must be tuned to your physical setup.

```
Servo angle 0° ────────────────────────────── 180°
                  ▲                    ▲
              pen UP angle         pen DOWN angle
              (e.g. 90°)           (e.g. 30°)
              stored as            stored as
              pcal_up_var          pcal_dn_var
```

### Calibration workflow

```
1. Click CALIBRATE button
   → Disables bounds enforcement
   → Sends PU to lift pen

2. Click POSE DOWN  → pen moves to DOWN angle
   Use NUDGE ▲▼ to fine-tune angle until pen just touches paper
   Click SET DOWN  → angle stored in firmware + saved locally

3. Click POSE UP  → pen moves to UP angle
   Use NUDGE ▲▼ to fine-tune angle until pen clears paper cleanly
   Click SET UP  → stored

4. Jog to far corner of bed  →  click SET X / SET Y to record limits

5. Click SAVE CALIB
   → calibration_ready = True
   → enforce_bounds re-enabled
   → All shapes/JSON plans now stay within these limits
```

PCAL commands are sent as `PCAL D<angle>` and `PCAL U<angle>`. The firmware responds with a `PEN_UP=<n> PEN_DOWN=<n>` info line before the `OK` ack, letting Python confirm the angles were accepted.

---

## GUI Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  CNC CONTROLLER  //  ARDUINO + TKINTER          [PORT] [CONNECT] │
│                                          [BAUD]         [STOP!]  │
├────────────────┬──────────────────────┬─────────────────────────┤
│   COL 1        │   COL 2              │   COL 3                 │
│  (scrollable)  │                      │                         │
│                │                      │  ┌─────────────────┐   │
│  MOVEMENT      │  DEMO PROGRAMS       │  │  DRAW PREVIEW   │   │
│  ┌─────────┐   │  [Calibrate]         │  │                 │   │
│  │↖  ↑  ↗ │   │  [Shapes tour]       │  │  Canvas         │   │
│  │←  ⊙  →│   │  [Spiral demo]       │  │  (drag to move) │   │
│  │↙  ↓  ↘ │   │  [Figure-8]          │  │                 │   │
│  └─────────┘   │  [Grid lines]        │  └─────────────────┘   │
│  Steps/Speed   │  [Star]              │  [✔ CONFIRM DRAW]       │
│                │  [Return home]       │  [CLEAR]                │
│  CALIBRATE     │                      │                         │
│  ZERO HERE     │  RAW COMMAND         │  SERIAL CONSOLE         │
│  MOVE UP/DN    │  [entry] [SEND]      │                         │
│                │                      │  << OK X=0 Y=0 PEN=UP   │
│  PEN SETUP     │  JSON DRAW API       │  >> PD                  │
│  UP°/DN° sliders│  [▶ PREVIEW JSON]   │  << OK X=0 Y=0 PEN=DOWN │
│  Nudge ▲▼      │  [json editor...]    │                         │
│                │                      │  [CLEAR]                │
│  SHAPES        │  COMMAND REFERENCE   │                         │
│  [SQUARE] [TR] │  (scrollable cheat   │                         │
│  [DIAMOND][RC] │   sheet)             │                         │
│  [ZIGZAG] [SP] │                      │                         │
└────────────────┴──────────────────────┴─────────────────────────┘
│  Motor X: 5,6,7,8  |  Motor Y: 9,10,11,12  |  Servo: 13        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Hardware Wiring

| Component | Arduino Pins |
|---|---|
| Stepper Motor X (IN1–IN4) | 5, 6, 7, 8 |
| Stepper Motor Y (IN1–IN4) | 9, 10, 11, 12 |
| Servo (signal) | 13 |
| Power | External motor driver supply |

Default baud rate: **9600**. Change `baud_combo` default in code if your firmware uses 115200.

---

## Installation

### Requirements

```bash
pip install pyserial
```

Python 3.8+ with `tkinter` (included in most Python distributions). On Ubuntu/Debian:

```bash
sudo apt install python3-tk
```

### Run

```bash
python cnc_controller.py
```

### First-time setup

1. Upload your Arduino firmware (set stepper pins and baud to match)
2. Run the controller, select your COM port, click **CONNECT**
3. Jog to verify axis directions — flip **Inv X**, **Inv Y**, or **Swap X/Y** checkboxes if needed
4. Click **CALIBRATE**, jog to the far corner, click SET X and SET Y
5. Fine-tune pen angles in **PEN SETUP**
6. Click **SAVE CALIB** — you're ready to draw

---

## Command Reference

### Motion commands

| Command | Description |
|---|---|
| `X<n>` | Move X axis n steps (negative = reverse) |
| `Y<n>` | Move Y axis n steps |
| `M<dx>,<dy>` | Diagonal move |
| `HOME` | Drive to origin (0,0) |
| `ZERO` | Mark current position as (0,0) |
| `STOP` | Emergency stop |

### Pen commands

| Command | Description |
|---|---|
| `PU` / `PD` | Pen up / pen down |
| `PCAL U<n>` | Set pen-up angle (0–180°) |
| `PCAL D<n>` | Set pen-down angle (0–180°) |
| `PCAL N<±n>` | Nudge servo ±n degrees |
| `PCAL?` | Query current angles |

### Built-in shapes (preview → confirm to draw)

| Command | Description |
|---|---|
| `SQ<n>` | Square, side = n steps |
| `TR<n>` | Triangle |
| `DM<n>` | Diamond |
| `RC<w>,<h>` | Rectangle |
| `ZZ<n>` | Zigzag (4 repeats) |
| `SP<n>,<rings>` | Expanding spiral |

### JSON draw

Paste a JSON spec into the editor and click **▶ PREVIEW JSON**, then **✔ CONFIRM DRAW**.

```json
{
  "auto_home": true,
  "fit": "contain",
  "margin": 10,
  "paths": [
    { "pen": "down", "closed": true,
      "points": [[0,0],[100,0],[100,100],[0,100]] }
  ]
}
```

### Pass-through

```
RAW <cmd>    Sends <cmd> directly to Arduino, bypasses safety checks
STATUS       Queries current position from firmware
```

---

## Architecture Notes

**Why `_send_command_wait()` instead of fire-and-forget?**  
The plotter's physical position must match `current_x`/`current_y`. If a command is lost or the Arduino rejects it (e.g. endstop hit), the PC would drift out of sync. Waiting for the `OK X=.. Y=..` ack and updating from it keeps everything consistent.

**Why an RLock for `motion_lock`?**  
`_run_draw_spec` holds `motion_lock` for the whole drawing. It internally calls `_set_pen` and `_move_to_absolute`, which also try to acquire `motion_lock`. A regular `Lock` would deadlock; `RLock` allows the same thread to re-enter.

**Why schedule Tkinter updates with `root.after(0, ...)`?**  
Tkinter is single-threaded. Calling `.config()`, `.insert()`, or any widget method from a background thread causes race conditions or crashes. `root.after(0, fn)` safely queues the call on the main event loop.

**Why is `_persist_state()` split into "snapshot + schedule"?**  
Reading `bound_x_var.get()` must happen on the main thread (it's a tkinter var). Writing the JSON file can happen anywhere. The function captures plain Python values from `state_lock`, then schedules the tkinter reads + file write together on the main thread.

---

## License

MIT — do whatever you want, just don't blame me if your pen runs off the bed.

---

*Built with Python 3, Tkinter, PySerial, and an Arduino that definitely doesn't miss steps.*
