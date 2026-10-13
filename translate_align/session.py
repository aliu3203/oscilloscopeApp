"""Keeps every raw capture and a JSON-lines log of each measurement, so a run can be re-analyzed later.

Files go to data/translate_align/<start time>/ in the repo (data/ is git-ignored):
    001_locate.npz, 002_cage_home.npz, ...   raw traces of each capture (scope.storage NPZ format)
    log.jsonl                                 one JSON record per measurement
"""

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from scope.storage import save_traces

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "translate_align"


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
        self._count = 0

    def save_point(self, label, traces):
        """Save one capture; returns the file name."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._count += 1
        name = f"{self._count:03d}_{label}.npz"
        save_traces(traces, self.dir / name)
        return name

    def log(self, record):
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"time": datetime.now().isoformat(timespec="seconds"), **record}
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps(_jsonable(record)) + "\n")
