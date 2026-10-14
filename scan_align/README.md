# Scan Align

Mini app that centers the PMQ doublet's magnetic centers on the pulsed wire by scanning one stage
axis at a time. At each stage position it measures each magnet's kick (its step on the photodiode
traces) and plots kick against position. A quadrupole's kick is linear in the wire's offset from its
magnetic center, so the points lie on a line, and **where the line crosses zero is the position to
move to**. Roll matching is not included.

```
python -m desktop.main          # start the capture app and Connect
python -m scan_align            # in another terminal, from the repo root
```

The app reaches the scope only through the capture app's local API.

## Before you start

- Set the scope to **NORMAL trigger on the current pulse**. In AUTO it also captures traces with no
  pulse, and the app warns about this.
- Set the timebase so both diodes' steps are on screen, and keep it fixed for the session. Changing it
  means locating again.
- Pick the two diode channels under **Photodiodes** and which magnet's step arrives first (PMQ2 on
  this bench).

## Workflow

1. **Locate steps.** With the wire visibly off-center, so both steps show on both diodes, press
   Capture & locate. The app finds the two steps on each diode and locks their times, so later
   steps can be measured all the way through zero and change sign. Locating again mid-scan
   re-measures the scan's points with the new times.
2. **Scan the cage stage in x.** Pick **Cage stage** and **x**. Set **Next position** to the stage
   reading, then **Collect** (one capture). Next position advances by **Step Δ**; move the stage there and
   Collect again. Four to six points across where PMQ2's kick changes sign is plenty. From two points
   on, **Zero crossing** says where to move the cage x.
3. **Move the cage x there, then New scan and do the cage y** the same way.
4. **Scan the PMQ1 mount in x, then in y** (Stage = **PMQ1 mount**), moving to each zero.
5. **Repeat once** from step 2. Each axis is centered with the other axis where it was, so a second
   round takes out what the first left. In simulation two rounds put both centers within 1 µm.

The cage stage moves both magnets, so it centers **PMQ2**. The PMQ1 mount moves PMQ1 only, so it then
centers **PMQ1**. A cage scan also shows where PMQ1's line crosses zero, which says how far the PMQ1
mount will have to move PMQ1. A mount scan checks the arrival order: if the step labelled PMQ2 is the
one that changes, the app says the order looks swapped.

## Reading the plots

- One plot per diode: kick vs. position, with error bars, for both magnets. The target magnet is
  filled with a solid line; the other is hollow with a dashed line.
- The answer uses the diode that pins the zero crossing best (usually the one whose kick changes most
  along this axis). A diode that barely responds to this axis shows a flat line and is ignored.
- The bottom plot is the last capture with its step fit, to check the step lock still holds.

## Warnings

- **Extrapolated**: the zero is well past the end of the scan. Move there and scan across it.
- **Points scatter more than their error bars**: a bad capture (Undo last) or a bent line (scan
  closer to the zero).
- **The diodes disagree** on the zero: the wire is off center along the other axis too. Center that
  axis, then rescan this one.

## Units

Positions only need to be consistent within a scan, so anything the stage shows works: µm, mm,
micrometer ticks, or **turns** of a knob with no scale (mark the knob and count turns). Moves are
reported in the same unit.

## Files

- `zero.py`: line fit, zero crossing and its uncertainty, and the warnings (numpy only). The steps
  themselves are measured by `translate_align/steps.py`. Long records are averaged down to 5 MS/s
  first, since the edges are 15–60 µs wide.
- `session.py`: saves every raw capture (NPZ), a CSV per scan, and `log.jsonl` to
  `data/scan_align/<start time>/`, which git ignores.
- `window.py`: the Qt window.
