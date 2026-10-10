# Oscilloscope Capture

Desktop app for capturing traces from a Siglent SDS1104X-E (default IP 10.0.0.237) and saving them locally.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```
python -m desktop.main
```

## Layout

- `scope/` – core instrument logic (no UI imports; reusable by a future web front end)
- `desktop/` – PySide6 UI; talks to the scope only through `scope/`

## Scope notes (SDS1104X-E)

- Raw SCPI over TCP port 5025; only the legacy command set works (`C1:VDIV?`, `TDIV?`, `C1:WF? DAT2`).
- The scope serves **one client at a time**: a new connection silently kills the previous one.
  Don't connect another script directly while the app is connected.
- Capture stops the scope, reads all selected channels from the same acquisition, then restores the trigger mode.

## Using the driver from Python

```python
from scope.siglent import SiglentScope

scope = SiglentScope("10.0.0.237")
scope.connect()
traces = scope.get_traces(["C2", "C4"])   # {"C2": Trace, "C4": Trace}
traces["C2"].time, traces["C2"].voltage, traces["C2"].metadata
scope.close()
```

## Saving

**Save…** writes the last capture to a path you choose: `.csv` (metadata in `#` header lines, then
`time_s,C1_V,...` columns) or `.npz` (compressed NumPy arrays + JSON metadata). The suggested filename
comes from `scope.storage.default_filename` — change the naming scheme there.

```python
from scope.storage import load_traces
d = load_traces("scope_20261009_193215.csv")   # or .npz
d["time"], d["C2"], d["metadata"]
```

## Local API (pull traces from scripts)

While the app is open and connected it serves an API on `http://127.0.0.1:8765` (local machine only).
Scripts must go through it — connecting to the scope directly would kick the app off.

One-time setup in the environment your scripts use (only needs numpy, not Qt):

```
pip install -e ~/dev/researchMusumeci/oscilloscopeApp
```

```python
from scope.client import ScopeClient
from scope.storage import save_traces

scope = ScopeClient()
scope.status()                      # connected, idn, trigger_mode, enabled_channels
traces = scope.capture(["C2", "C4"])  # fresh capture; None = all enabled channels
traces["C2"].time, traces["C2"].voltage, traces["C2"].metadata
scope.get_trace("C2")               # single Trace
scope.last()                        # most recent capture (app or script), no new acquisition
scope.query("C2:VDIV?")             # read-only query -> "C2:VDIV 1.00E-03V"
scope.run(); scope.stop()           # resume / stop acquisition
save_traces(traces, "run_001.npz")
```

Captures made through the API also appear in the app's plot and become the Save target.
See `examples/pull_traces.py`.

| Endpoint | Description |
|---|---|
| `GET /status` | connection state, trigger mode, enabled channels |
| `GET /capture?channels=C2,C4&freeze=1&format=npz\|json` | fresh capture |
| `GET /last?format=npz\|json` | most recent capture |
| `GET /query?cmd=TDIV?` | read-only text query (binary queries like `WF?` are rejected) |
| `GET /run`, `GET /stop` | resume / stop acquisition |

Non-Python: `curl "http://127.0.0.1:8765/capture?channels=C2&format=json"` or in MATLAB
`d = webread("http://127.0.0.1:8765/capture?channels=C2&format=json");`

**Known issue:** capturing immediately after resuming (or many captures back-to-back) can catch the scope
before it has a finished acquisition; the read times out and the scope is left stopped. Press **Run**
(or `scope.run()`) to recover, and leave a short pause after `run()` before capturing.
