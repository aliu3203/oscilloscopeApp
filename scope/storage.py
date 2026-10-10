"""Save captured traces to disk.

CSV: '#' comment lines with metadata, then columns time_s,C2_V,C3_V,...
     Also readable with pandas.read_csv(path, comment="#").
NPZ: arrays 'time' and one per channel ('C2', ...), plus 'metadata' as a JSON string.

Both load back with load_traces(path) -> {"time": array, "C2": array, ..., "metadata": dict}.
"""

import json
from datetime import datetime
from pathlib import Path

import numpy as np

SHARED_KEYS = ("instrument", "captured_at", "trigger_mode", "tdiv", "trigger_delay", "sample_rate", "n_points")
CHANNEL_KEYS = ("vdiv", "offset")


def default_filename(traces, ext="csv"):
    """Suggested filename for a capture. Change the naming scheme here."""
    return f"scope_{datetime.now():%Y%m%d_%H%M%S}.{ext}"


def _shared_time(traces):
    traces = list(traces.values())
    time = traces[0].time
    for t in traces[1:]:
        if len(t.time) != len(time) or not np.allclose(t.time, time):
            raise ValueError("Traces do not share a time axis; capture them together to save in one file")
    return time


def _metadata(traces):
    first = next(iter(traces.values())).metadata
    meta = {k: first[k] for k in SHARED_KEYS if k in first}
    meta["channels"] = {ch: {k: t.metadata[k] for k in CHANNEL_KEYS if k in t.metadata} for ch, t in traces.items()}
    return meta


def save_csv(traces, path):
    time = _shared_time(traces)
    meta = _metadata(traces)
    header = [f"{k}: {meta[k]}" for k in SHARED_KEYS if k in meta]
    header += [f"{ch}: vdiv={m.get('vdiv')} V/div, offset={m.get('offset')} V" for ch, m in meta["channels"].items()]
    columns = ",".join(["time_s"] + [f"{ch}_V" for ch in traces])

    data = np.column_stack([time] + [t.voltage for t in traces.values()])
    fmt = ["%.10g"] + ["%.6g"] * len(traces)
    with open(path, "w", newline="") as f:
        f.writelines(f"# {line}\n" for line in header)
        f.write(columns + "\n")
        np.savetxt(f, data, delimiter=",", fmt=fmt)


def save_npz(traces, path):
    arrays = {"time": _shared_time(traces)}
    arrays.update({ch: t.voltage for ch, t in traces.items()})
    np.savez_compressed(path, metadata=json.dumps(_metadata(traces)), **arrays)


def save_traces(traces, path):
    """Save traces to `path`; format chosen by extension (.csv or .npz). Returns the path written."""
    if not traces:
        raise ValueError("Nothing to save")
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        save_csv(traces, path)
    elif suffix == ".npz":
        save_npz(traces, path)
    else:
        raise ValueError(f"Unsupported file type {suffix!r}; use .csv or .npz")
    return path


def load_traces(path):
    """Load a file written by save_traces. Returns {"time": ..., "<channel>": ..., "metadata": ...}.

    For CSV files, metadata values are the raw header strings.
    """
    path = Path(path)
    if path.suffix.lower() == ".npz":
        with np.load(path) as z:
            out = {k: z[k] for k in z.files if k != "metadata"}
            out["metadata"] = json.loads(str(z["metadata"]))
        return out

    metadata = {}
    with open(path) as f:
        for line in f:
            if not line.startswith("#"):
                columns = line.strip().split(",")
                break
            key, _, value = line[1:].strip().partition(": ")
            metadata[key] = value
        data = np.loadtxt(f, delimiter=",", ndmin=2)
    out = {"time": data[:, 0]}
    out.update({col.removesuffix("_V"): data[:, i] for i, col in enumerate(columns) if i > 0})
    out["metadata"] = metadata
    return out
