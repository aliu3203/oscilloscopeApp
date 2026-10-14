"""Keeps every raw capture, a CSV per scan, and a JSON-lines log, so a scan can be re-analyzed later.

Files go to data/scan_align/<start time>/ in the repo (data/ is git-ignored):
    locate.npz                      raw traces of the Locate capture
    scan01_p003.npz                 raw traces of point 3 of scan 1 (scope.storage NPZ format)
    scan01_cage_x.csv               position and each magnet's kick on each diode (rewritten on every collect)
    log.jsonl                       one JSON record per locate, point and analysis
"""

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from scope.storage import save_traces

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "scan_align"


def _jsonable(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if is_dataclass(obj):
        return {k: _jsonable(v) for k, v in asdict(obj).items() if not isinstance(v, np.ndarray) or v.size <= 4}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


class Session:
    def __init__(self, root=DATA_DIR):
        self.dir = Path(root) / f"{datetime.now():%Y%m%d_%H%M%S}"

    def save_capture(self, name, traces):
        """Save one capture as <name>.npz; returns the file name."""
        self.dir.mkdir(parents=True, exist_ok=True)
        save_traces(traces, self.dir / f"{name}.npz")
        return f"{name}.npz"

    def save_scan(self, scan, stage, axis, unit, roles, points):
        self.dir.mkdir(parents=True, exist_ok=True)
        magnets = list(points[0].steps) if points else []
        cols = [f"{m}_{r}_{q}" for m in magnets for r in roles for q in ("mV", "err_mV")]
        with open(self.dir / f"scan{scan:02d}_{stage}_{axis}.csv", "w") as f:
            f.write(",".join([f"x_{unit}", *cols]) + "\n")
            for p in points:
                values = [v for m in magnets for k in range(len(roles)) for v in (p.steps[m][0][k], p.steps[m][1][k])]
                f.write(",".join([f"{p.x:.6g}", *(f"{v:.6f}" for v in values)]) + "\n")

    def log(self, record):
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"time": datetime.now().isoformat(timespec="seconds"), **record}
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps(_jsonable(record)) + "\n")
