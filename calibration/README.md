# Photodiode Calibration

Mini app that finds the photodiode position where its response to position is steepest. Scan the
diode in steps of Δx from an origin, collect its mean voltage at each step, and the app tells you the
offset from the origin where the slope dV/dx is largest. Run it once per photodiode.

```
python -m desktop.main          # start the capture app and Connect
python -m calibration           # in another terminal, from the repo root
```

The app reaches the scope only through the capture app's local API.

## Workflow

1. Pick the photodiode's **Channel**. Each Collect is exactly one capture, and the point is the mean
   over every sample in it. The app never fires captures back to back: that can catch the scope
   without a finished acquisition and leave it stopped. To average more, Collect again at the same
   position (set Next position back); repeated positions are averaged.
2. Set the **Step Δx** and **Unit**. Positions only need to be relative, so stage µm or micrometer
   ticks both work. A negative step scans the other way.
3. Put the diode at the origin (**Next position** = 0) and press **Collect**. Next position then
   advances by Δx. Move the diode there and Collect again. You can type any Next position to fill
   in a gap or go back.
4. From three positions on, the top plot shows mean voltage against position and the bottom one
   shows the slope between neighboring points. **Steepest response** gives the offset from the
   origin to move to, and how far that is from the last point.

**Undo last** drops the last point. **New scan** starts again from 0. Start a new scan when you
change diodes; the channel is locked while a scan has points.

## How the peak is found

No response shape is assumed. The steepest segment between neighboring points is found first. A
cubic is then fitted to that segment and up to two points on each side, and its inflection gives the
peak between scan points (the dashed curve on the top plot). If that fails, the app falls back to
the segment's midpoint and says so. The app also warns when the peak sits at the end of the scan
(extend the scan) and when the steepest change is within noise. When the response has two flanks
(e.g. a shadow crossing the diode), the opposite-sign flank is reported too.

## Files

- `gradient.py`: per-point mean and uncertainty, and the slope and its peak (numpy only).
- `session.py`: saves each raw capture (NPZ), a CSV per scan, and `log.jsonl` to
  `data/calibration/<start time>/`, which git ignores.
- `window.py`: the Qt window.
