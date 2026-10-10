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
