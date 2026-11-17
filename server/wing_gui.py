#!/usr/bin/env python3
"""WING GUI - a local web app over the same engine Claude drives.

    python3 wing_gui.py              # opens http://127.0.0.1:8765 in a browser
    python3 wing_gui.py --lan        # also reachable from an iPad on the LAN
    python3 wing_gui.py --port 9000 --no-browser

Standard library only. Every button calls the very functions the MCP tools
are (server.py), so validation, exclusions, read-back, staged model changes
and push retries are identical whether Claude or a person drives the desk -
one implementation, three front doors (Claude, the CLI scripts, this page).

Two caveats:
  - This runs as its OWN process, separate from the copy Claude Desktop
    starts. Each enforces one background job at a time within itself, but
    not across the two. Don't push from here while Claude is pushing.
  - --lan serves the page with no login, to anyone on the network. That is
    no wider than the desk itself (its OSC is unauthenticated), but it is a
    convenient way in; use it on a network you trust.
"""

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server as S  # noqa: E402

TOOLS = {
    "verify_connection": S.verify_connection,
    "wing_list_consoles": S.wing_list_consoles,
    "wing_connect": S.wing_connect,
    "wing_get": S.wing_get,
    "wing_find": S.wing_find,
    "wing_set": S.wing_set,
    "wing_dump": S.wing_dump,
    "wing_push_file": S.wing_push_file,
    "wing_job_status": S.wing_job_status,
    "wing_generate_profile": S.wing_generate_profile,
    "wing_meters": S.wing_meters,
    "wing_rta": S.wing_rta,
    "wing_gain_check": S.wing_gain_check,
    "wing_set_many": S.wing_set_many,
    "wing_models": S.wing_models,
    "wing_fade": S.wing_fade,
    "wing_fade_status": S.wing_fade_status,
    "wing_fade_stop": S.wing_fade_stop,
    "wing_watch": S.wing_watch,
    "wing_changes": S.wing_changes,
    "wing_preset_save": S.wing_preset_save,
    "wing_preset_list": S.wing_preset_list,
    "wing_preset_load": S.wing_preset_load,
    "wing_scene_status": S.wing_scene_status,
    "wing_audio_check": S.wing_audio_check,
    "wing_loudness": S.wing_loudness,
    "wing_loudness_monitor": S.wing_loudness_monitor,
    "wing_loudness_status": S.wing_loudness_status,
    "wing_polarity_check": S.wing_polarity_check,
    "wing_feedback_find": S.wing_feedback_find,
    "wing_pa_measure": S.wing_pa_measure,
    "wing_pa_list": S.wing_pa_list,
    "wing_pa_report": S.wing_pa_report,
    "wing_pa_average": S.wing_pa_average,
    "wing_pa_eq": S.wing_pa_eq,
    "wing_pa_align": S.wing_pa_align,
    "wing_pa_room": S.wing_pa_room,
}

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "wing-gui"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE_PATH, "rb") as f:
                return self._send(200, f.read(), "text/html")
        return self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send(404, json.dumps({"ok": False, "error": "not found"}))
        name = self.path[5:]
        fn = TOOLS.get(name)
        if fn is None:
            return self._send(404, json.dumps({"ok": False, "error": "no tool %s" % name}))
        # Same-origin only: a page on another site must not be able to drive
        # the desk through a browser that happens to have this open.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin.split("://", 1)[-1] != host:
            return self._send(403, json.dumps({"ok": False, "error": "cross-origin request refused"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(args, dict):
                raise ValueError("body must be a JSON object")
            res = fn(**args)
        except TypeError as exc:
            res = {"ok": False, "error": "bad arguments: %s" % exc}
        except Exception as exc:
            res = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        self._send(200, json.dumps(res, default=str))


def main():
    ap = argparse.ArgumentParser(description="WING local web GUI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="listen on all interfaces (iPad/phone on the same network)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, a.port), Handler)
    url = "http://127.0.0.1:%d" % a.port
    print("WING GUI on %s%s  (Ctrl-C to stop)" % (
        url, "  - and on this machine's LAN address" if a.lan else ""))
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
