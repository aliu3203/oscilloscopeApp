"""Pull traces from the running Oscilloscope Capture app and save them.

Start the app and click Connect first. Run with: python examples/pull_traces.py
"""

from scope.client import ScopeClient
from scope.storage import save_traces

scope = ScopeClient()

status = scope.status()
print(f"Connected: {status['connected']}  mode: {status['trigger_mode']}  channels on: {status['enabled_channels']}")

traces = scope.capture(["C2", "C4"])  # freezes the scope, reads both channels, resumes
for ch, trace in traces.items():
    print(f"{ch}: {len(trace)} points, {trace.voltage.min():.4g} to {trace.voltage.max():.4g} V")

path = save_traces(traces, "example_capture.csv")
print(f"Saved {path}")
