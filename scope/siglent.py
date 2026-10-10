"""Driver for Siglent SDS1000X-E scopes over raw SCPI (TCP port 5025).

This scope only understands the legacy command set (C1:VDIV?, TDIV?, C1:WF? DAT2, ...)
and serves a single TCP client: opening a second connection silently kills the first.
All public methods are guarded by a lock so one instance can be shared across threads.
"""

import re
import socket
import threading

from scope.trace import codes_to_trace

CHANNELS = ("C1", "C2", "C3", "C4")
_NUMBER = re.compile(r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)([kMG]?)")
_PREFIX = {"": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}


class ScopeError(Exception):
    pass


def normalize_channel(channel):
    """Accept 2, "2", "C2", "CH2", "ch2" and return "C2"."""
    s = str(channel).strip().upper()
    if s.startswith("CH"):
        s = s[2:]
    elif s.startswith("C"):
        s = s[1:]
    name = f"C{s}"
    if name not in CHANNELS:
        raise ScopeError(f"Unknown channel: {channel!r}")
    return name


class SiglentScope:
    def __init__(self, host, port=5025, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.idn = None
        self._sock = None
        self._buf = b""
        self._lock = threading.RLock()

    # --- connection ---------------------------------------------------------

    @property
    def is_connected(self):
        return self._sock is not None

    def connect(self):
        with self._lock:
            self.close()
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except OSError as e:
                self._sock = None
                raise ScopeError(f"Could not connect to {self.host}:{self.port}: {e}") from e
            self._buf = b""
            self._write("CHDR SHORT")
            self.idn = self._query("*IDN?")

    def close(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None
            self._buf = b""

    @property
    def model(self):
        parts = (self.idn or "").split(",")
        return parts[1] if len(parts) > 1 else (self.idn or "")

    # --- low-level I/O ------------------------------------------------------

    def _fail(self, message, exc=None):
        # After a timeout or socket error the response stream may be out of sync, so drop the connection.
        self.close()
        raise ScopeError(f"{message} (connection closed; reconnect to continue)") from exc

    def _write(self, cmd):
        if self._sock is None:
            raise ScopeError("Not connected")
        try:
            self._sock.sendall((cmd + "\n").encode())
        except OSError as e:
            self._fail(f"Send failed for {cmd!r}: {e}", e)

    def _recv_more(self, cmd):
        try:
            chunk = self._sock.recv(1 << 16)
        except OSError as e:
            self._fail(f"No response to {cmd!r}: {e}", e)
        if not chunk:
            self._fail(f"Scope closed the connection during {cmd!r}")
        self._buf += chunk

    def _query(self, cmd):
        """Send a query and return its response line, skipping stray blank lines left by block transfers."""
        self._write(cmd)
        while True:
            while b"\n" not in self._buf:
                self._recv_more(cmd)
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line:
                return line.decode(errors="replace")

    def _query_value(self, cmd):
        """Parse a numeric response like 'C2:VDIV 1.00E-03V' or 'SARA 1.00E+06Sa/s'."""
        response = self._query(cmd)
        value_part = response.split(" ", 1)[-1]
        match = _NUMBER.search(value_part)
        if not match:
            raise ScopeError(f"Could not parse number from {response!r}")
        return float(match.group(1)) * _PREFIX[match.group(2)]

    def _query_block(self, cmd):
        """Send a query that returns an IEEE 488.2 definite-length block (#<n><len><data>)."""
        self._write(cmd)
        while b"#" not in self._buf:
            self._recv_more(cmd)
        start = self._buf.index(b"#")
        while len(self._buf) < start + 2:
            self._recv_more(cmd)
        n_digits = int(self._buf[start + 1:start + 2])
        header_end = start + 2 + n_digits
        while len(self._buf) < header_end:
            self._recv_more(cmd)
        length = int(self._buf[start + 2:header_end])
        while len(self._buf) < header_end + length:
            self._recv_more(cmd)
        data = self._buf[header_end:header_end + length]
        # Trailing newlines are dropped here or skipped by the next _query.
        self._buf = self._buf[header_end + length:].lstrip(b"\n")
        return data

    # --- public API ---------------------------------------------------------

    def query(self, cmd):
        """Send an arbitrary query and return the response string."""
        with self._lock:
            return self._query(cmd)

    def enabled_channels(self):
        with self._lock:
            return [ch for ch in CHANNELS if self._query(f"{ch}:TRA?").split()[-1] == "ON"]

    def get_traces(self, channels, freeze=True):
        """Fetch several channels. With freeze=True the scope is stopped so all channels come
        from the same acquisition, then the previous trigger mode is restored."""
        names = [normalize_channel(ch) for ch in channels]
        if not names:
            raise ScopeError("No channels selected")

        with self._lock:
            enabled = self.enabled_channels()
            disabled = [ch for ch in names if ch not in enabled]
            if disabled:
                raise ScopeError(f"Channel(s) {', '.join(disabled)} are turned off on the scope")

            previous_mode = self._query("TRMD?").split()[-1]
            stopped = freeze and previous_mode != "STOP"
            if stopped:
                self._write("STOP")
                self._query("TRMD?")  # round-trip so the STOP has taken effect before reading
            try:
                tdiv = self._query_value("TDIV?")
                trdl = self._query_value("TRDL?")
                sara = self._query_value("SARA?")
                traces = {}
                for ch in names:
                    vdiv = self._query_value(f"{ch}:VDIV?")
                    ofst = self._query_value(f"{ch}:OFST?")
                    raw = self._query_block(f"{ch}:WF? DAT2")
                    traces[ch] = codes_to_trace(
                        raw, ch, vdiv, ofst, tdiv, trdl, sara,
                        extra_metadata={"instrument": self.idn, "trigger_mode": previous_mode},
                    )
            finally:
                if stopped and self.is_connected:
                    self._write(f"TRMD {previous_mode}")
            return traces

    def get_trace(self, channel, freeze=True):
        name = normalize_channel(channel)
        return self.get_traces([name], freeze=freeze)[name]
