# Scenario files

A scenario is a JSON file that drives apps on screen and records clips while it does. `gifcap run scene.json`
runs it and prints a timeline: every step's time inside the clip, plus any `mark`s. Use those times for
`gifcap gif --trim` and `--speed`.

```json
{
  "name": "nda-risk-summary",
  "steps": [
    {"focus": "Contract Coach"},
    {"record_start": {"window": "Contract Coach", "pad": 8}},
    {"click": {"window": "Contract Coach", "frac": [0.5, 0.92]}},
    {"type": "What are the key risks in this NDA?"},
    {"mark": "asked"},
    {"keys": "enter"},
    {"wait_idle": {"quiet": 2, "timeout": 90}},
    {"mark": "answered"},
    {"record_stop": {}}
  ]
}
```

Then, for example: `gifcap gif takes/nda-risk-summary.mkv out/01-nda-risk-summary.gif --trim 0.5- --speed 4.6-31.2=4`
(times from the timeline's `marks`).

## Steps

Each step is an object with exactly one key.

| Step | Value | Notes |
| --- | --- | --- |
| `focus` | window | Brings it to the front and makes it the **typing target**. |
| `record_start` | `{window \| region \| monitor, pad, fps, cursor, name}` | `region` is `[x, y, w, h]`; `monitor` 1 is the primary. One clip at a time; a scenario can have several. |
| `record_stop` | `{after}` | Keeps recording `after` seconds (default 0.6) so the last frame lingers. |
| `click`, `double_click`, `right_click`, `move` | point | Clicking into a window also makes it the typing target. |
| `drag` | `{from: point, to: point, duration}` | |
| `type` | text, or `{text, cps, window}` | Human speed by default (12 characters/second). `\n` presses Enter. |
| `keys` | combo, or `{keys, window}` | `"ctrl+v"`, `"ctrl+a delete"`, `"win+shift+d"`. |
| `wait` | seconds | |
| `wait_idle` | `{window \| region \| monitor, quiet, timeout}` | Waits until nothing changes for `quiet` seconds: the way to wait for an AI reply to finish. Defaults to the clip's area. |
| `mark` | name | Records the current time in the clip. |
| `screenshot` | name, or `{name, window \| region \| monitor}` | PNG for one-pagers and decks. |

**Points:** `{"window": "Notepad", "frac": [0.5, 0.3]}` (fraction of the window), `{"window": "Notepad", "px": [40, 120]}`
(pixels from its top-left), or `{"x": 900, "y": 400}` (screen pixels). Windows match by title or process name,
case-insensitive; `gifcap windows` lists them.

## Safety

- **Keystrokes only go to the typing target.** `type` and `keys` fail unless a `focus` step (or a click into a window)
  came first, and `gifcap` checks before *every* keystroke that the target is still in front. If anything else takes
  focus, it stops with an error instead of typing into the wrong app. Global hotkeys (`win+…`) are exempt.
- Use demo-safe sample data. Before sharing, review a contact sheet (`gifcap sheet`) for anything confidential and
  cover it with `--redact x,y,w,h` (solid box, unrecoverable) rather than `--blur`.

## Tips

- Times in the timeline are accurate to about ±0.3 s; trim a little generously.
- Windows can move between scenario steps; record a `window` that won't be dragged, or use a `region`.
- Screen recording and simulated input need to run outside an agent's sandbox. Expect an approval prompt.
