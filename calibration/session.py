"""Keeps every raw capture, a CSV per scan, and a JSON-lines log, so a scan can be re-analyzed later.

Files go to data/calibration/<start time>/ in the repo (data/ is git-ignored):
    scan01_C2.csv               position, mean, uncertainty of each point in scan 1 (rewritten on every collect)
    scan01_p003.npz             raw trace of point 3 (scope.storage NPZ format)
    log.jsonl                   one JSON record per point and per analysis
"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from scope.storage import save_traces

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "calibration"


class Session:
    def __init__(self, root=DATA_DIR):
        self.dir = Path(root) / f"{datetime.now():%Y%m%d_%H%M%S}"

    def save_capture(self, scan, point, traces):
        """Save one capture; returns the file name."""
        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"scan{scan:02d}_p{point:03d}.npz"
        save_traces(traces, self.dir / name)
        return name

    def save_scan(self, scan, channel, unit, points):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f"scan{scan:02d}_{channel}.csv", "w") as f:
            f.write(f"x_{unit},mean_mV,err_mV,noise_mV\n")
            for p in points:
                f.write(f"{p.x:.6g},{p.mean_mv:.6f},{p.err_mv:.6f},{p.noise_mv:.6f}\n")

    def log(self, record):
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"time": datetime.now().isoformat(timespec="seconds"), **record}
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps(record, default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o)) + "\n")
