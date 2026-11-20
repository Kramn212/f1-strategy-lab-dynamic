"""
server.py - serves the dashboard AND the refresh API with nothing but the Python standard library.

Run:  python pipeline/server.py          ->  http://localhost:8000
      (PORT=... changes the port; REFRESH_KEY=... requires a password for the Refresh button)

Endpoints
---------
GET  /                    the dashboard (public/)
GET  /data/...            the JSON files written by the pipeline (always fresh, never cached)
GET  /api/status          {state, message, done, total, last_result, ...}
POST /api/refresh         start a refresh in the BACKGROUND (returns at once)
                          401 wrong/missing key, 409 already running, 429 cooldown

Why a background job? Downloading even one race takes a minute or more because OpenF1 limits
requests to roughly 27 a minute, far longer than a web request may last. The button starts the
job and the page polls /api/status to show progress.

Protection: only one refresh runs at a time, there is a cooldown between refreshes
(config.REFRESH_COOLDOWN_S) and each click loads at most config.MAX_PER_REFRESH races, so nobody
can use the button to hammer OpenF1. Set REFRESH_KEY to restrict it to people who know the key.
"""
import json
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import config
import run

LOCK = threading.Lock()
STATE = {"state": "idle", "message": "", "done": 0, "total": 0, "started": None, "finished": None,
         "last_result": None, "error": None}


def snapshot():
    with LOCK:
        s = dict(STATE)
    s["requires_key"] = bool(os.environ.get("REFRESH_KEY"))
    s["cooldown_s"] = config.REFRESH_COOLDOWN_S
    s["server_time"] = time.time()
    return s


def _progress(message, done=None, total=None):
    with LOCK:
        STATE["message"] = message
        if done is not None:
            STATE["done"], STATE["total"] = done, total


def _job():
    try:
        result = run.refresh(progress=_progress, limit=config.MAX_PER_REFRESH)
        with LOCK:
            STATE.update(state="done", last_result=result, error=None, message="Finished")
    except Exception as exc:                                  # show the problem in the page
        with LOCK:
            STATE.update(state="error", error=str(exc), message="Refresh failed")
    finally:
        with LOCK:
            STATE["finished"] = time.time()


def start_refresh(key=None):
    """Returns (http_status, payload). Starts the background job when allowed."""
    need = os.environ.get("REFRESH_KEY")
    if need and key != need:
        return 401, {"error": "Refresh key required or wrong."}
    with LOCK:
        if STATE["state"] == "running":
            return 409, {"error": "A refresh is already running."}
        wait = (STATE["finished"] or 0) + config.REFRESH_COOLDOWN_S - time.time()
        if wait > 0:
            return 429, {"error": f"Please wait {int(wait)} s before refreshing again.", "retry_after": int(wait)}
        STATE.update(state="running", message="Starting…", done=0, total=0, started=time.time(),
                     error=None, last_result=None)
    threading.Thread(target=_job, daemon=True).start()
    return 202, {"started": True}


class Handler(SimpleHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def translate_path(self, path):
        """/data/* is served from config.OUT_DIR (which can live on a persistent disk); the rest from public/."""
        p = unquote(urlparse(path).path)
        if p.startswith("/data/"):
            # drop '.', '..' and anything path-like so a request can never escape the data folder
            parts = [x for x in p[len("/data/"):].split("/") if x not in ("", ".", "..") and "\\" not in x and ":" not in x]
            return os.path.join(str(config.OUT_DIR), *parts)
        return super().translate_path(path)

    def end_headers(self):
        if urlparse(self.path).path.startswith("/data/"):
            self.send_header("Cache-Control", "no-cache")          # always revalidate: new data shows at once
        super().end_headers()

    def do_GET(self):
        if urlparse(self.path).path == "/api/status":
            return self._json(200, snapshot())
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            code, payload = start_refresh(self.headers.get("X-Refresh-Key"))
            return self._json(code, payload)
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):                              # quieter console
        if "/api/" in (str(args[0]) if args else ""):
            super().log_message(fmt, *args)


def make_server(port=0, host="127.0.0.1"):
    return ThreadingHTTPServer((host, port), partial(Handler, directory=str(config.PUBLIC_DIR)))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"      # hosting platforms set PORT
    print(f"Serving on http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    make_server(port, host).serve_forever()
