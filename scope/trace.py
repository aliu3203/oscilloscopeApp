from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

# The SDS1000X-E screen is 14 divisions wide and an ADC code of 25 equals one vertical division.
H_DIVS = 14
CODES_PER_DIV = 25


@dataclass
class Trace:
    channel: str
    time: np.ndarray
    voltage: np.ndarray
    metadata: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.voltage)


def codes_to_trace(raw, channel, vdiv, offset, tdiv, trdl, sample_rate, extra_metadata=None):
    """Convert raw signed 8-bit waveform codes into time (s) and voltage (V) arrays."""
    codes = np.frombuffer(raw, dtype=np.int8).astype(np.float64)
    voltage = codes * (vdiv / CODES_PER_DIV) - offset
    t0 = -(H_DIVS / 2) * tdiv - trdl
    time = t0 + np.arange(len(codes)) / sample_rate

    metadata = {
        "channel": channel,
        "vdiv": vdiv,
        "offset": offset,
        "tdiv": tdiv,
        "trigger_delay": trdl,
        "sample_rate": sample_rate,
        "n_points": len(codes),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return Trace(channel, time, voltage, metadata)
