"""Client for the app's local API. Use this from scripts instead of connecting to the scope directly.

    from scope.client import ScopeClient
    scope = ScopeClient()
    traces = scope.capture(["C2", "C4"])     # {"C2": Trace, "C4": Trace}
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from scope.api_server import DEFAULT_API_PORT
from scope.siglent import ScopeError, normalize_channel
from scope.storage import traces_from_npz_bytes


class ScopeClient:
    def __init__(self, url=f"http://127.0.0.1:{DEFAULT_API_PORT}", timeout=120):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _get(self, path, **params):
        full_url = f"{self.url}{path}?{urlencode(params)}" if params else f"{self.url}{path}"
        try:
            with urlopen(full_url, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as e:
            try:
                message = json.loads(e.read())["error"]
            except (ValueError, KeyError):
                message = f"HTTP {e.code}"
            raise ScopeError(message) from None
        except URLError as e:
            raise ScopeError(f"App not reachable at {self.url} (is it running?): {e.reason}") from None

    def status(self):
        """{"connected": bool, "idn": str, "trigger_mode": str, "enabled_channels": [...], "api_version": int}"""
        return json.loads(self._get("/status"))

    def capture(self, channels=None, freeze=True):
        """Fresh capture. channels: e.g. ["C2", 4]; None captures every enabled channel.
        Returns {channel: Trace}."""
        params = {"freeze": int(freeze), "format": "npz"}
        if channels:
            params["channels"] = ",".join(normalize_channel(c) for c in channels)
        return traces_from_npz_bytes(self._get("/capture", **params))

    def get_trace(self, channel, freeze=True):
        name = normalize_channel(channel)
        return self.capture([name], freeze=freeze)[name]

    def last(self):
        """Most recent capture made in the app or by any script, without triggering a new one."""
        return traces_from_npz_bytes(self._get("/last", format="npz"))

    def query(self, cmd):
        """Read-only scope query, e.g. query("C2:VDIV?") -> "C2:VDIV 1.00E-03V"."""
        return json.loads(self._get("/query", cmd=cmd))["response"]

    def run(self):
        """Resume acquisition (last running trigger mode, AUTO by default). Returns the new mode."""
        return json.loads(self._get("/run"))["trigger_mode"]

    def stop(self):
        """Stop acquisition. Returns the new mode ("STOP")."""
        return json.loads(self._get("/stop"))["trigger_mode"]
