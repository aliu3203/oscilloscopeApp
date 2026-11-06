# Roll Align

Mini app that matches PMQ2's roll (it sits in the rotation mount) to PMQ1's. It compares how each
magnet's step changes when the cage moves the wire between two points, A and B, and scans PMQ2's
rotation to find where the two changes line up. This is the change-vector test of the "Pulsed-Wire
Doublet Alignment" write-up (§7.3).

```
python -m desktop.main          # start the capture app and Connect
python -m roll_align            # in another terminal, from the repo root
```

The app reaches the scope only through the capture app's local API, one capture per button press.

## Theory

Each magnet adds a step to each photodiode trace: `step_i = D · GL_i · S(θ_i) · d_i`, where `d_i` is
the wire's offset from magnet i's center, `θ_i` its roll, `GL_i` its strength, and `D` the unknown
diode response (gains and directions).

- **B − A.** Moving the cage moves both magnets by the same Δs, so in the difference trace the unknown
  offsets cancel: `ΔS_i = GL_i · D · S(θ_i) · Δs`. Anything that does not depend on the wire's
  position cancels too.
- **The ratio test.** When the rolls match, `S(θ₂) = ±S(θ₁)`, so ΔS₂ is a multiple of ΔS₁: the two
  changes lie on one line and dH₁/dV₁ = dH₂/dV₂. The same D acts on both, so no diode calibration is
  needed. This holds for any A, B and any move direction.
- **Angles instead of ratios.** dH/dV blows up when dV is near zero. The angle of each change's line
  carries the same information; "lines differ by" is the ratio test in that form.
- **The 90° blind spot.** Turning a quadrupole 90° only flips its kick, so the ratios also match with
  PMQ2 90° away. A focusing–defocusing doublet makes the two changes point in **opposite** directions;
  the same-way setting focuses both magnets in the same plane. The app measures the full angle from
  ΔS₁ to ΔS₂ and reports **off target**: that angle less the target (180° for opposite).
- **The scan.** Turning PMQ2 by φ turns its change by 2φ, so off target vs. PMQ2 reading is close to a
  straight line with slope about 2°/°. **Where it crosses zero is the reading to set.** Uncalibrated
  diodes change the slope and bend the line away from the zero, but not where the zero is. The bending
  depends on the move direction, so use the same A and B (or swap them) every pair.

This matches PMQ2's roll **to PMQ1's**. The doublet's overall roll relative to the beamline is set by
PMQ1 and is not measured here; that needs diodes with known directions (write-up §7.4).

## Workflow

1. Pick the diode channels (**Diode H**, **Diode V**) and which magnet's step arrives first (PMQ2 on
   this bench). Target: **Opposite** for a focusing–defocusing doublet.
2. **Step times.** The steps' times and edge widths are located on the first pair's difference and then
   locked, and the lock is kept across restarts. **Use an earlier lock…** reloads one from an earlier
   Roll Align or Scan Align session. Check that the dotted lines sit on the edges of the difference
   trace. A step that barely changes on one diode cannot be located there, so keep a good lock rather
   than locating again.
3. Choose two cage positions A and B a few tenths of a mm apart (diagonal changes both steps on both
   diodes). Move only the cage between A and B; leave the PMQ1 mount and the anchors alone.
4. Set **Unit**, **Step Δ**, **Pairs per reading**, and **PMQ2 reading** to what the mount reads.
5. **Capture A**, move the cage, **Capture B**. Repeat at the same reading until it has its pairs
   (each pair can start where the cage is: A and B may swap). The reading then advances by Step Δ:
   turn PMQ2 there and continue. Capturing B again replaces that pair's B; **Undo last** drops a pair.
6. Scan coarse first (10–15° steps) if the zero is far, then **New scan** and scan finely around it.
   Set PMQ2 to the zero and confirm with a few pairs there.
7. Turning PMQ2 moves its magnetic center slightly: re-center both magnets with Scan Align afterwards.

## Error bars

A single pair's error comes from its step fit and assumes white noise; on the bench it is several times
smaller than the real scatter between repeated pairs. With **Pairs per reading** of 2 or more, each
reading is plotted as the mean of its pairs (filled) with the pairs themselves (hollow), and its error
bar is the pair-to-pair SD pooled over all readings, divided by √n. Pooling matters: a 3-pair SD alone
varies by a factor of 2 or more by chance. The zero's uncertainty comes from the line fit of the
reading means with those errors.

## Files

- `compare.py`: difference of two captures, the bounded step refinement, the roll comparison, and the
  scan fit with per-reading averaging (numpy only). Steps are measured by `translate_align/steps.py`.
- `window.py`: the Qt window.
- Saved to `data/roll_align/<start time>/` (git-ignored): every capture (NPZ), `log.jsonl` (captures,
  locks, every pair's result), `scanNN_roll.csv` (one row per pair) and `scanNN_roll_readings.csv`
  (one row per reading: mean, error, n, SD).
