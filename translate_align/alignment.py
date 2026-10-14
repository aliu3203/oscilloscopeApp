"""Turn measured steps into stage moves (numpy only, no Qt).

Follows the "Pulsed-Wire Doublet Alignment" write-up, §6-8. S1 and S2 are the PMQ1 and PMQ2 steps as
2-vectors (diode X, diode Y) in mV. Moving a stage by s (µm, along the stage's own +x/+y as bumped
during calibration) changes them linearly, S_i(s) = S_i(0) + J_i s, with J_i = GL_i D S(theta_i) M_stage.
The detector matrix D is never needed: calibration measures each J_i directly.

    cage stage  moves both magnets   -> null PMQ2's step with it (phase 1)
    PMQ1 mount  moves PMQ1 only      -> then null PMQ1's step with it (phase 2)

Never null the end level (S1 + S2): it leaves the wire off both centers while reading zero (§6.3).
"""

from dataclasses import dataclass, field

import numpy as np

MAGNETS = ("PMQ1", "PMQ2")
ILL_CONDITIONED = 30.0  # warn when a response matrix is this close to singular


@dataclass
class Calibration:
    stage: str  # "cage" or "mount"
    J: dict  # magnet -> 2x2 response, mV per µm (rows: diode X, Y; columns: stage x, y)
    S0: dict  # magnet -> fitted step at the calibration origin, mV
    rms_mv: float  # scatter of the points about the linear model (0 with exactly 3 points)
    bump_um: float


@dataclass
class Recommendation:
    cage_um: np.ndarray  # cage move that nulls PMQ2's step
    cage_err_um: np.ndarray
    S1_after_cage: np.ndarray  # predicted PMQ1 step after the cage move
    mount_um: np.ndarray  # PMQ1 mount move that then nulls PMQ1's step
    mount_err_um: np.ndarray
    mount_estimated: bool  # True when the mount is not calibrated (assumes its axes match the cage's)
    notes: list = field(default_factory=list)


def step_vectors(fits_by_role, roles, arrival_order=MAGNETS):
    """Per-diode fits -> {magnet: (S, err)}; S and err are 2-vectors over `roles`, in mV.

    fits_by_role: {role: StepFit}; the first-arriving step belongs to arrival_order[0].
    """
    out = {}
    for k, magnet in enumerate(arrival_order):
        S = np.array([fits_by_role[r].s1 if k == 0 else fits_by_role[r].s2 for r in roles])
        E = np.array([fits_by_role[r].e1 if k == 0 else fits_by_role[r].e2 for r in roles])
        out[magnet] = (S, E)
    return out


def fit_calibration(stage, offsets_um, steps, bump_um):
    """Least-squares S = S0 + J s over >= 3 non-collinear stage offsets.

    offsets_um: [(x, y), ...]; steps: [{magnet: (S, err)}, ...] from step_vectors.
    """
    A = np.column_stack([np.ones(len(offsets_um)), np.asarray(offsets_um, float)])
    if np.linalg.matrix_rank(A) < 3:
        raise ValueError("Calibration needs three stage positions that are not on one line")
    J, S0, resid = {}, {}, []
    for magnet in MAGNETS:
        Y = np.array([point[magnet][0] for point in steps])
        coef, *_ = np.linalg.lstsq(A, Y, rcond=None)
        S0[magnet], J[magnet] = coef[0], coef[1:].T
        resid.append(Y - A @ coef)
    rms = float(np.sqrt(np.mean(np.square(resid)))) if len(offsets_um) > 3 else 0.0
    return Calibration(stage, J, S0, rms, bump_um)


def coupling(mount_cal, cage_cal):
    """How much PMQ2's step moves when only the PMQ1 mount moves, relative to a cage move (ideally 0)."""
    return float(np.linalg.norm(mount_cal.J["PMQ2"]) / np.linalg.norm(cage_cal.J["PMQ2"]))


def _solve_null(J, S, err):
    """Move s with J s = -S, and its 1-sigma uncertainty from the step uncertainties."""
    Jinv = np.linalg.inv(J)
    move = -Jinv @ S
    move_err = np.sqrt(np.diag(Jinv @ np.diag(np.square(err)) @ Jinv.T))
    return move, move_err


def recommend(steps, cage, mount=None):
    """Cage move to null PMQ2's step, then PMQ1-mount move to null PMQ1's (§8.1 phases 1 and 2)."""
    (S1, E1), (S2, E2) = steps["PMQ1"], steps["PMQ2"]
    notes = []
    for name, J in (("cage, PMQ2", cage.J["PMQ2"]), ("cage, PMQ1", cage.J["PMQ1"])):
        if np.linalg.cond(J) > ILL_CONDITIONED:
            notes.append(f"Response ({name}) is nearly singular (cond {np.linalg.cond(J):.0f}): one diode barely sees "
                         "one stage axis, so that part of the move is poorly determined.")

    cage_um, cage_err = _solve_null(cage.J["PMQ2"], S2, E2)
    S1_after = S1 + cage.J["PMQ1"] @ cage_um
    J_mount = mount.J["PMQ1"] if mount else cage.J["PMQ1"]
    mount_um, mount_err = _solve_null(J_mount, S1_after, E1)

    for name, move, cal in (("Cage", cage_um, cage), ("PMQ1 mount", mount_um, mount or cage)):
        if np.max(np.abs(move)) > 2 * cal.bump_um:
            notes.append(f"{name} move is beyond twice the {cal.bump_um:.0f} µm calibration bump: the response may not be "
                         "linear that far. Move part way and re-measure.")
    if mount is None:
        notes.append("PMQ1 mount move is an estimate that assumes the mount's x/y match the cage stage's. "
                     "Calibrate the mount for an exact answer.")
    return Recommendation(cage_um, cage_err, S1_after, mount_um, mount_err, mount is None, notes)
