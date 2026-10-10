"""Local HTTP API so external scripts can pull traces through the app's single scope connection.

The scope serves only one TCP client, so scripts must not connect to it directly while the app is
connected. Instead the app runs this server (bound to 127.0.0.1) and scripts use scope.client.ScopeClient.

Endpoints (GET):
  /status                                       connection state and enabled channels
  /capture?channels=C2,C4&freeze=1&format=npz   fresh capture (channels omitted -> all enabled)
  /last?format=npz                              most recent capture from the app or any script
  /query?cmd=TDIV?                              read-only text query passthrough
  /run, /stop                                   resume / stop acquisition
format is "npz" (default, binary) or "json".
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from scope.siglent import ScopeError
from scope.storage import traces_to_json, traces_to_npz_bytes

API_VERSION = 1
DEFAULT_API_PORT = 8765

# Queries whose reply is a binary block would desync the text reader.
BINARY_QUERIES = ("WF?", "SCDP", "PNSU?", "*LRN?")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ApiServer:
    def __init__(self, get_scope, on_capture=None, host="127.0.0.1", port=DEFAULT_API_PORT):
        """get_scope() returns the connected SiglentScope or None.
        on_capture(traces) is called (from a server thread) after each API capture."""
        self.get_scope = get_scope
        self.on_capture = on_capture
        self.host = host
        self.port = port
        self.last_traces = {}
        self._httpd = None
        self._thread = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"

    def start(self):
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.api = self
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="scope-api", daemon=True)
        self._thread.start()

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # --- endpoint logic -----------------------------------------------------

    def _require_scope(self):
        scope = self.get_scope()
        if scope is None or not scope.is_connected:
            raise ApiError(503, "Scope not connected in the app")
        return scope

    def status(self):
        scope = self.get_scope()
        connected = scope is not None and scope.is_connected
        return {
            "api_version": API_VERSION,
            "connected": connected,
            "idn": scope.idn if connected else None,
            "trigger_mode": scope.trigger_mode() if connected else None,
            "enabled_channels": scope.enabled_channels() if connected else [],
        }

    def capture(self, channels, freeze):
        scope = self._require_scope()
        if not channels:
            channels = scope.enabled_channels()
        traces = scope.get_traces(channels, freeze=freeze)
        self.last_traces = traces
        if self.on_capture is not None:
            self.on_capture(traces)
        return traces

    def last(self):
        if not self.last_traces:
            raise ApiError(404, "Nothing has been captured yet")
        return self.last_traces

    def query(self, cmd):
        cmd = cmd.strip()
        upper = cmd.upper()
        if not cmd.endswith("?") or any(c in cmd for c in ";\n\r"):
            raise ApiError(400, "Only single read-only queries ending in '?' are allowed")
        if any(b in upper for b in BINARY_QUERIES):
            raise ApiError(400, f"{cmd!r} returns binary data; use /capture for waveforms")
        return self._require_scope().query(cmd)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        api = self.server.api
        url = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/status":
                self._send_json(api.status())
            elif url.path == "/capture":
                channels = [c for c in params.get("channels", "").split(",") if c.strip()]
                freeze = params.get("freeze", "1").lower() not in ("0", "false", "no")
                self._send_traces(api.capture(channels, freeze), params.get("format", "npz"))
            elif url.path == "/last":
                self._send_traces(api.last(), params.get("format", "npz"))
            elif url.path == "/run":
                self._send_json({"trigger_mode": api._require_scope().run()})
            elif url.path == "/stop":
                self._send_json({"trigger_mode": api._require_scope().stop()})
            elif url.path == "/query":
                self._send_json({"response": api.query(params.get("cmd", ""))})
            else:
                raise ApiError(404, f"Unknown endpoint {url.path}")
        except ApiError as e:
            self._send_json({"error": str(e)}, e.status)
        except (ScopeError, ValueError) as e:
            scope = api.get_scope()
            status = 503 if scope is None or not scope.is_connected else 400
            self._send_json({"error": str(e)}, status)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _send_traces(self, traces, fmt):
        if fmt == "json":
            self._send_json(traces_to_json(traces))
        elif fmt == "npz":
            self._send(traces_to_npz_bytes(traces), "application/octet-stream")
        else:
            raise ApiError(400, f"Unknown format {fmt!r}; use npz or json")

    def _send_json(self, obj, status=200):
        self._send(json.dumps(obj).encode(), "application/json", status)

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
