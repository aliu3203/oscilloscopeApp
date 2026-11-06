"""Find where a magnet's kick crosses zero along one stage axis (numpy only, no Qt).

A scan moves one stage axis through a series of positions. At each one, a single capture gives each
magnet's step (its kick) on both photodiodes, measured with the step times locked
(translate_align.steps). For a quadrupole the kick is linear in the wire's offset from the magnetic
center, so kick vs. position is a straight line, and where it crosses zero is the position that puts
that magnet's center on the wire along this axis.

Scanning x zeroes the kick along x with y wherever it is. The diodes' axes, the magnet's roll and the
stage's axes are not lined up, so that zero leaves a y error, and the y scan then leaves a smaller x
error. Each x-then-y round multiplies the error by rho = (J_xy J_yx) / (J_xx J_yy), where J is the
2x2 response (rows: the diode used for x, the diode used for y; columns: stage x, y). That needs the
right diode on each axis: the swapped pairing has rho -> 1/rho and diverges. One x scan plus one y
scan give all four slopes, so center_2d solves for the magnetic center directly instead.

Positions are the stage readings, in whatever unit the stage reads (µm, mm, ticks, turns). Kicks are in mV.
"""

from dataclasses import dataclass, field

import numpy as np

MAX_RATE = 5e6  # traces are block-averaged down to this sample rate before fitting (edges are 15-60 µs wide)
DISAGREE_SIGMA = 3.0  # flag when the two diodes' zero crossings differ by more than this
SCATTER_CHI2 = 4.0  # flag when the points scatter this much more than their error bars
EXTRAPOLATE = 0.1  # flag a zero crossing further past the end of the scan than this fraction of its span


def decimate(trace, max_rate=MAX_RATE):
    """Block-average a trace down to about max_rate. Long records (700k points) are slow to fit."""
    dt = trace.time[1] - trace.time[0]
    k = int(1 / (dt * max_rate))
    if k <= 1:
        return trace
    n = len(trace.time) // k * k
    avg = lambda a: a[:n].reshape(-1, k).mean(axis=1)  # noqa: E731
    return type(trace)(trace.channel, avg(trace.time), avg(trace.voltage), trace.metadata)


@dataclass
class LineFit:
    slope: float  # mV per unit
    slope_err: float
    x0: float  # where the line crosses zero; nan if the slope is zero
    x0_err: float
    chi2: float  # chi-square per degree of freedom; nan with only two positions
    n: int
    xm: float  # weighted mean position of the points
    level: float  # the line's value at xm, mV (uncorrelated with the slope)
    level_err: float

    @property
    def significant(self):
        """True when the slope is clearly non-zero, so the zero crossing means something."""
        return np.isfinite(self.x0) and abs(self.slope) > 3 * self.slope_err


def fit_line(x, y, err):
    """Weighted straight-line fit and its zero crossing. Needs two distinct positions.

    When the points scatter more than their error bars (chi2 > 1), the uncertainties are scaled up by
    sqrt(chi2), so a bad capture shows up as a larger error instead of a falsely precise answer.
    """
    x, y, err = (np.asarray(a, float) for a in (x, y, err))
    if np.unique(x).size < 2:
        raise ValueError("Need at least two different positions to fit a line")
    w = 1 / np.maximum(err, 1e-9) ** 2
    xm = np.sum(w * x) / w.sum()
    u = x - xm  # centred, so intercept and slope are uncorrelated
    a = np.sum(w * y) / w.sum()
    b = np.sum(w * u * y) / np.sum(w * u**2)
    var_a, var_b = 1 / w.sum(), 1 / np.sum(w * u**2)
    dof = len(x) - 2
    chi2 = float(np.sum(w * (y - a - b * u) ** 2) / dof) if dof > 0 else float("nan")
    scale = max(1.0, chi2) if dof > 0 else 1.0
    a_err, b_err = float(np.sqrt(scale * var_a)), float(np.sqrt(scale * var_b))
    if b == 0:
        x0, x0_err = float("nan"), float("nan")
    else:
        x0 = float(xm - a / b)
        x0_err = float(np.sqrt(scale * (var_a / b**2 + a**2 * var_b / b**4)))
    return LineFit(float(b), b_err, x0, x0_err, chi2, len(x), float(xm), float(a), a_err)


@dataclass
class ScanPoint:
    x: float
    steps: dict  # magnet -> (S, err): 2-vectors over the diode roles, mV (translate_align.alignment.step_vectors)


@dataclass
class ScanResult:
    fits: dict  # magnet -> {role: LineFit}
    target: str  # the magnet this stage centers
    role: str  # the diode whose zero crossing is the answer (None if it has no clear slope)
    x0: float
    x0_err: float
    notes: list = field(default_factory=list)


def fit_all(points, roles):
    """{magnet: {role: LineFit}}: every magnet's kick against position, on every diode."""
    x = [p.x for p in points]
    return {magnet: {r: fit_line(x, [p.steps[magnet][0][k] for p in points], [p.steps[magnet][1][k] for p in points])
                     for k, r in enumerate(roles)}
            for magnet in points[0].steps}


def analyze(points, roles, target, role):
    """Line fit of every magnet's kick on every diode, and the target's zero crossing on diode `role`."""
    x = np.array([p.x for p in points])
    fits = fit_all(points, roles)
    notes = []
    best = fits[target][role]
    if not best.significant:
        others = [r for r in roles if r != role and fits[target][r].significant]
        notes.append(f"{target}'s kick on {role} shows no clear slope along this axis"
                     + (f" ({others[0]} does: its zero is listed, but iterating with it may not converge)." if others
                        else ": widen the scan (bigger step), or check that the steps are still locked (last capture)."))
        return ScanResult(fits, target, None, float("nan"), float("nan"), notes)

    lo, hi = x.min(), x.max()
    past = max(best.x0 - hi, lo - best.x0)
    if past > max(EXTRAPOLATE * (hi - lo), 2 * best.x0_err):
        notes.append(f"The zero crossing is {past:.3g} past the end of the scan, so it is extrapolated. "
                     "Move there and add points on both sides of it to confirm.")
    if np.isfinite(best.chi2) and best.chi2 > SCATTER_CHI2:
        notes.append(f"The points scatter {np.sqrt(best.chi2):.1f}× more than their error bars: look for a bad "
                     "capture (Undo last) or a bent line (the scan is too wide; scan closer to the zero).")
    for r in roles:
        f = fits[target][r]
        if r != role and f.significant and abs(f.x0 - best.x0) > DISAGREE_SIGMA * np.hypot(f.x0_err, best.x0_err):
            notes.append(f"The diodes disagree on the zero: the wire is off center along the other axis too. "
                         f"Center that axis, then rescan this one (or use the 2×2 center).")
    return ScanResult(fits, target, role, best.x0, best.x0_err, notes)


@dataclass
class Center:
    """A magnet's center from one x scan and one y scan of the same stage."""

    magnet: str
    J: np.ndarray  # mV per unit; rows: diodes (roles order), columns: stage x, y
    x: float  # stage reading that puts the center on the wire
    y: float
    x_err: float
    y_err: float
    rho: float  # per-round error factor of x-then-y scans with diode roles[0] on x and roles[1] on y
    cond: float  # condition number of J: large means one diode barely tells the axes apart


def center_2d(magnet, fits_x, fits_y, roles, samples=4000):
    """Solve S = J (p - p*) for the center p*, from the x scan's and y scan's line fits of one magnet.

    The x scan gives J's first column (slopes along x) and, at its mean position, both diodes' kicks:
    J^-1 of those is the wire's offset from the center at that point, whose x part pins x* in stage
    readings. The y scan does the same for y*. Neither needs the other axis's reading during the scan.
    Uncertainties come from resampling every slope and level within its error.
    """
    fx, fy = [fits_x[r] for r in roles], [fits_y[r] for r in roles]

    def solve(sx, sy, lx, ly):
        J = np.stack([np.stack([sx[..., 0], sy[..., 0]], -1), np.stack([sx[..., 1], sy[..., 1]], -1)], -2)
        Jinv = np.linalg.inv(J)
        dx = np.einsum("...ij,...j->...i", Jinv, lx)  # offset (p - p*) at the x scan's mean position
        dy = np.einsum("...ij,...j->...i", Jinv, ly)
        return J, fx[0].xm - dx[..., 0], fy[0].xm - dy[..., 1]

    val = lambda fits, attr: np.array([getattr(f, attr) for f in fits])  # noqa: E731
    J, x, y = solve(val(fx, "slope"), val(fy, "slope"), val(fx, "level"), val(fy, "level"))
    rng = np.random.default_rng(0)
    draw = lambda fits, attr: val(fits, attr) + val(fits, attr + "_err") * rng.standard_normal((samples, len(fits)))  # noqa: E731
    _, xs, ys = solve(draw(fx, "slope"), draw(fy, "slope"), draw(fx, "level"), draw(fy, "level"))
    ok = np.isfinite(xs) & np.isfinite(ys)
    # Robust spread (the inverse has heavy tails when a slope's error is not small)
    spread = lambda a: float(1.4826 * np.median(np.abs(a - np.median(a))))  # noqa: E731
    rho = J[0, 1] * J[1, 0] / (J[0, 0] * J[1, 1])
    return Center(magnet, J, float(x), float(y), spread(xs[ok]), spread(ys[ok]), float(rho), float(np.linalg.cond(J)))
