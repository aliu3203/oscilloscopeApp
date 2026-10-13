# Translate Align

Mini app that centers the PMQ doublet's magnetic centers on the pulsed wire (phases 1–2 of the
"Pulsed-Wire Doublet Alignment" write-up). It finds each magnet's step on both photodiodes and tells
you how far to move the cage stage and the PMQ1 mount. Roll matching is not included yet.

```
python -m desktop.main          # start the capture app and Connect
python -m translate_align       # in another terminal, from the repo root
```

The app reaches the scope only through the capture app's local API.

## Before you start

- Set the scope to **NORMAL trigger on the current pulse**. In AUTO it also captures traces with no pulse,
  and the app warns about this.
- Set the timebase so both diodes' steps are on screen, and keep it fixed for the whole session.
  Changing it means locating again.
- Pick the two diode channels under **Photodiodes**. Each button press is one capture of both diodes
  from the same acquisition, made with the capture app's own `capture` call.

## Workflow

1. **Locate steps.** Start with the wire visibly off-center so both steps show on both diodes. The app
   finds the two steps on each diode and locks their times. This capture is also the **home point**:
   note the cage stage and PMQ1 mount readings. The diodes have different times on this
   bench, so each channel is located separately. **First step to arrive** says which magnet's step
   comes first. It depends on which side of the cage the diodes sit, and on this bench it is PMQ2.
   The check: a cage move from the app should center PMQ2. If it centers PMQ1 instead, flip the
   setting. The mount calibration also warns when the order looks swapped.
2. **Calibrate the cage stage.** Move +x from home by the bump (default 50 µm) and Capture point, then
   back to home x and +y by the bump and Capture point. Return to home afterwards. The Locate capture is
   the home point, so the stages must not have moved since then. This measures how each step responds to each stage axis, so diode gains
   and angles never need to be known. Optionally calibrate the **PMQ1 mount** the same way. Without
   it, the mount move is an estimate that assumes the mount's axes match the cage's.
3. **Capture & recommend.** The app measures both steps and prints:
   1. the cage move that nulls **PMQ2's** step;
   2. the PMQ1-mount move that then nulls **PMQ1's** step (the mount moves PMQ1 only).

   Make both moves, then capture again. Repeat until the moves are within their ± uncertainties.

"+x/+y" always means the directions you moved during calibration. The app never nulls the end level
(the sum of the two steps): that leaves the wire off both centers while the signal reads zero (§6.3).

## Files

- `steps.py`: finds and measures the steps. It removes the ~30.9 kHz pickup tone and fits a
  baseline, drift and two smooth steps.
- `alignment.py`: calibration (S = S0 + J·s) and the recommended moves.
- `session.py`: saves every raw capture (NPZ) and a `log.jsonl` of each measurement to
  `data/translate_align/<start time>/`, which git ignores.
- `window.py`: the Qt window.
