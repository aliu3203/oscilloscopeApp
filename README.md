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
