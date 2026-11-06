"""Relative roll of the two PMQs from two captures at different wire positions (numpy only, no Qt).

Put the wire at two off-axis points A and B (move the cage stage, which moves both magnets the same
way), capture at each, and subtract the traces point by point (B - A). Whatever does not depend on the
wire's position cancels, and each magnet's step in the difference trace is the change of its kick:

    dS_i = GL_i D S(theta_i) ds        (write-up §7.3; D = the diodes, ds = the move)

Both magnets see the same move through the same diodes, so when the rolls match, dS_2 is a multiple
of dS_1: the ratios dH/dV of the two magnets are equal. Rolling a quadrupole 90° flips its kick, so
the test is blind to 90° (and to which magnet is stronger); it only asks whether the two change-vectors
lie on one line. The ratio blows up when dV is near zero, so the same comparison is also given as the
angle between the two lines, which stays well behaved and is what to drive to zero. With calibrated
diodes that angle is twice the relative roll; with uncalibrated ones it is near that and still crosses
zero at matched rolls.

Times are in µs and voltages in mV, as in translate_align.steps.
"""

from dataclasses import dataclass, replace

import numpy as np

from scan_align.zero import EXTRAPOLATE, SCATTER_CHI2, fit_line
from translate_align.steps import measure_steps

REFINE_SHIFT_US = 15.0  # the refinement may move each step time at most this far from where it was located
REFINE_WIDTHS_US = (10.0, 260.0)  # and keeps each edge width within this range


def difference(a, b):
    """Point-by-point B - A of two captures ({channel: Trace}); both must share the time axis."""
    out = {}
    for ch, tb in b.items():
        ta = a[ch]
        if len(ta.time) != len(tb.time) or not np.allclose(ta.time, tb.time, rtol=0, atol=1e-3 * (ta.time[1] - ta.time[0])):
            raise ValueError(f"{ch}: A and B have different time axes (timebase, delay or memory depth changed?). Capture both again.")
        out[ch] = type(tb)(ch, tb.time, tb.voltage - ta.voltage, tb.metadata)
    return out


def line_angle(v):
    """Angle of the line through vector v = (dH, dV), in degrees within [-90, 90): v and -v give the same."""
    return float((np.degrees(np.arctan2(v[1], v[0])) + 90) % 180 - 90)


def _angle_err(v, e):
    """1-sigma error of the angle of v (degrees), from the errors e of its components."""
    n2 = v @ v
    return float(np.degrees(np.sqrt((v[1] * e[0]) ** 2 + (v[0] * e[1]) ** 2) / n2)) if n2 > 0 else float("inf")


def _ratio(v, e):
    """dH/dV and its 1-sigma error; inf when dV is zero."""
    if v[1] == 0:
        return float("inf"), float("inf")
    r = v[0] / v[1]
    return float(r), float(np.hypot(e[0] / v[1], v[0] * e[1] / v[1] ** 2))


def refine_model(trace, model, rounds=6):
    """Step times and widths refined to the least residual, starting from a located model.

    locate_steps picks widths from a coarse grid. A width that is off by 10 µs leaks a little of one
    step into the other, by a different amount on each diode, and that turns the change-vectors: in
    simulation it shifted the angle by 0.7°, about half a degree of roll. The difference trace has large
    steps, so the times and widths can be pinned much better than the grid. Times stay within
    REFINE_SHIFT_US of the located ones and widths within REFINE_WIDTHS_US, so a step that is small on
    one diode cannot pull its edge onto a bump in the noise.
    """
    best = model
    bounds = {"t1": (model.t1 - REFINE_SHIFT_US, model.t1 + REFINE_SHIFT_US),
              "t2": (model.t2 - REFINE_SHIFT_US, model.t2 + REFINE_SHIFT_US),
              "w1": REFINE_WIDTHS_US, "w2": REFINE_WIDTHS_US}
    cost = measure_steps(trace, best).resid_uv
    steps = {"t1": 4.0, "w1": 8.0, "t2": 4.0, "w2": 8.0}
    for _ in range(rounds):
        for name, step in steps.items():
            for sign in (1, -1):
                while True:
                    value = getattr(best, name) + sign * step
                    lo, hi = bounds[name]
                    if not lo <= value <= hi:
                        break
                    trial = replace(best, **{name: value})
                    c = measure_steps(trace, trial).resid_uv
                    if c >= cost:
                        break
                    best, cost = trial, c
        steps = {k: v / 2 for k, v in steps.items()}
    return best


@dataclass
class RollResult:
    fixed: str  # the magnet with no rotation mount (PMQ1)
    adjustable: str  # the magnet in the rotation mount (PMQ2)
    dS: dict  # magnet -> np.array([dH, dV]), mV
    err: dict  # magnet -> np.array([eH, eV]), mV
    ratio: dict  # magnet -> (dH/dV, err)
    angle: dict  # magnet -> (line angle of dS in degrees, err)
    mismatch: float  # angle from the fixed magnet's line to the adjustable one's, degrees in [-90, 90); 0 = rolls match
    mismatch_err: float
    turn: float  # angle from dS_fixed to dS_adjustable as vectors, degrees in [-180, 180); 180 = opposite, 0 = same way
    size_ratio: float  # |dS_adjustable| / |dS_fixed|
    opposite: bool  # True when the two change-vectors point more opposite than the same way
    notes: list

    @property
    def roll_estimate(self):
        """About how far the adjustable magnet's roll is off, in degrees (half the mismatch; sign not known)."""
        return abs(self.mismatch) / 2

    def off_target(self, target):
        """turn minus the target (180: opposite, focusing-defocusing; 0: same way), in [-180, 180).

        Unlike the ratio test, this tells the two matched settings apart: they are 90° of PMQ2 apart.
        It is unchanged by swapping A and B, which flips both vectors.
        """
        return wrap(self.turn - target, 360)


def wrap(angle, period):
    """angle in [-period/2, period/2)."""
    return float((angle + period / 2) % period - period / 2)


MIN_SIGMA = 5.0  # a change-vector shorter than this many sigmas cannot give a direction


def compare(fits, roles, arrival_order, fixed="PMQ1", adjustable="PMQ2"):
    """Roll comparison from the step fits of a difference trace.

    fits: {role: StepFit} on the difference trace, roles = (horizontal diode, vertical diode);
    arrival_order: the magnet whose step comes first, then the other.
    """
    dS, err = {}, {}
    for k, magnet in enumerate(arrival_order):
        dS[magnet] = np.array([fits[r].s1 if k == 0 else fits[r].s2 for r in roles])
        err[magnet] = np.array([fits[r].e1 if k == 0 else fits[r].e2 for r in roles])
    ratio = {m: _ratio(dS[m], err[m]) for m in dS}
    angle = {m: (line_angle(dS[m]), _angle_err(dS[m], err[m])) for m in dS}
    mismatch = wrap(angle[adjustable][0] - angle[fixed][0], 180)
    mismatch_err = float(np.hypot(angle[adjustable][1], angle[fixed][1]))
    turn = wrap(np.degrees(np.arctan2(dS[adjustable][1], dS[adjustable][0]) - np.arctan2(dS[fixed][1], dS[fixed][0])), 360)
    n1, n2 = np.linalg.norm(dS[fixed]), np.linalg.norm(dS[adjustable])
    notes = []
    for m in dS:
        if np.linalg.norm(dS[m]) < MIN_SIGMA * np.linalg.norm(err[m]):
            notes.append(f"{m}'s step barely changed between A and B, so its direction is noise: move A and B further apart.")
    for m, (r, _) in ratio.items():
        if abs(dS[m][1]) < 3 * err[m][1]:
            notes.append(f"{m}'s dV is close to zero, so its ratio dH/dV is unreliable; compare the angles instead.")
    return RollResult(fixed, adjustable, dS, err, ratio, angle, mismatch, mismatch_err, turn,
                      float(n2 / n1) if n1 > 0 else float("inf"), bool(abs(turn) > 90), notes)


def measure_difference(diff, channels, models, arrival_order, fixed="PMQ1", adjustable="PMQ2"):
    """Fit both steps on each diode's difference trace with the locked times; returns (fits, RollResult).

    channels: {role: channel} in (horizontal, vertical) order; models: {role: StepModel}.
    """
    fits = {role: measure_steps(diff[ch], models[role]) for role, ch in channels.items()}
    return fits, compare(fits, list(channels), arrival_order, fixed, adjustable)


@dataclass
class Reading:
    """The pairs taken at one PMQ2 reading, averaged."""

    reading: float
    mean: float  # mean off-target angle (degrees)
    err: float  # standard error of the mean: pooled pair-to-pair SD / sqrt(n)
    n: int
    sd: float  # pair-to-pair standard deviation; nan with one pair
    from_repeats: bool  # err comes from the scatter of repeated pairs, not one trace's fit


@dataclass
class RollScan:
    """Angle off target vs. PMQ2 reading over one rotation scan, and where it crosses zero."""

    readings: np.ndarray  # per pair, in the order taken
    angles: np.ndarray  # off-target angle of each pair, unwrapped along the scan (degrees)
    errs: np.ndarray  # each pair's fit error (white-noise estimate: an underestimate)
    groups: list  # Reading per distinct PMQ2 reading, in the order first taken
    pooled_sd: float  # pair-to-pair scatter pooled over the readings with repeats; nan without repeats
    fit: object  # scan_align.zero.LineFit of the reading means, or None with fewer than two readings
    zero: float  # reading where the fitted line reaches the nearest multiple of 360° (a matched setting); nan if none
    zero_err: float
    notes: list


def group_readings(x, y, e):
    """Average the pairs taken at each reading. Returns (groups, pooled_sd).

    A pair's fit error assumes white noise and comes out several times smaller than the real scatter
    between repeated pairs, so the error of a reading comes from the repeats. Every reading is taken the
    same way, so the scatter is pooled over all readings with repeats (a 3-pair SD alone varies by a
    factor of 2 or more by chance) and each reading gets pooled SD / sqrt(n). Without any repeats it falls
    back to the fit errors.
    """
    keys = list(dict.fromkeys(np.round(x, 9)))
    sel = [np.round(x, 9) == k for k in keys]
    dof = sum(m.sum() - 1 for m in sel)
    pooled = float(np.sqrt(sum(((y[m] - y[m].mean()) ** 2).sum() for m in sel) / dof)) if dof > 0 else float("nan")
    groups = []
    for k, m in zip(keys, sel):
        n = int(m.sum())
        sd = float(np.std(y[m], ddof=1)) if n > 1 else float("nan")
        fit_err = float(np.sqrt((e[m] ** 2).sum()) / n)
        if np.isfinite(pooled):
            err, repeats = max(pooled / np.sqrt(n), fit_err), True
        else:
            err, repeats = fit_err, False
        groups.append(Reading(float(k), float(y[m].mean()), float(err), n, sd, repeats))
    return groups, pooled


def fit_scan(readings, results, target):
    """Fit the off-target angle against the PMQ2 reading and find the reading that zeroes it.

    readings and results are in the order taken; several pairs at one reading are averaged first
    (group_readings). Each angle is unwrapped onto the previous one, so a scan may pass through ±180°.
    Turning PMQ2 by 1° turns its change-vector by about 2° (exactly 2° with calibrated diodes), so the
    slope also checks the reading's unit and sign.
    """
    x = np.asarray(readings, float)
    y = np.array([r.off_target(target) for r in results])
    for k in range(1, len(y)):
        y[k] = y[k - 1] + wrap(y[k] - y[k - 1], 360)
    e = np.array([max(r.mismatch_err, 1e-3) for r in results])
    groups, pooled = group_readings(x, y, e)
    notes = []
    if not all(g.from_repeats for g in groups):
        notes.append("Error bars are from single traces and come out several times too small: take two or more pairs "
                     "at a reading (Pairs per reading) for real ones.")
    if len(groups) < 2:
        return RollScan(x, y, e, groups, pooled, None, float("nan"), float("nan"), notes)
    gx = np.array([g.reading for g in groups])
    fit = fit_line(gx, [g.mean for g in groups], [g.err for g in groups])
    if not fit.significant:
        notes.append("The angle shows no clear trend with the reading: turn PMQ2 further between readings, or check that "
                     "the reading is entered before each pair.")
        return RollScan(x, y, e, groups, pooled, fit, float("nan"), float("nan"), notes)
    k = round((fit.level + fit.slope * (gx.mean() - fit.xm)) / 360)  # nearest matched setting to the data
    zero = float(fit.x0 + 360 * k / fit.slope)
    lo, hi = gx.min(), gx.max()
    if max(zero - hi, lo - zero) > max(EXTRAPOLATE * (hi - lo), 2 * fit.x0_err):
        notes.append("The zero is past the end of the scan, so it is extrapolated, and the line bends far from it. "
                     "Turn PMQ2 there and scan across it with smaller steps.")
    if np.isfinite(fit.chi2) and fit.chi2 > SCATTER_CHI2:
        notes.append(f"The readings scatter {np.sqrt(fit.chi2):.1f}× more than their error bars: a bad pair (Undo last), "
                     "A and B moved between readings, or the line bends (scan closer to the zero).")
    return RollScan(x, y, e, groups, pooled, fit, zero, float(fit.x0_err), notes)
