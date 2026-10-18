#!/usr/bin/env python3
"""Stand-in engine for the native app's checks that the real engine cannot stage.
Stdlib only, made-up data, 127.0.0.1 only. Run by the `network-crm-native-standin` launch
config on 8869; `NC_NATIVE_PORT=8869 … --selftest` then runs only the checks that need it
(SelfTest+AskLists.swift, SelfTest+Engine.swift).

Ask's Stop: POST /api/chat takes ANSWER_SECONDS (20) unless a POST /api/chat/stop names
its run id; then it waits STOP_DELAY (3 s, like the real engine finishing its round) and
answers stopped:true. Like the real engine (api_ask.py, NC-052) the question is in the
chat's history from the moment the request arrives. GET /stub/log says which run ids each
route saw.

The engine going away and coming back: GET /stub/pause?seconds=N makes /api/status answer
503 for N seconds. The database moved aside: GET /stub/recovery?text=… puts that sentence
in /api/status `db_recovery` until POST /api/db-recovery-notice/dismiss. Like the engine
since NC-342, a GET of that address changes nothing (it falls through to the {} answer
below), so an app that still dismisses with a GET leaves the sentence in place and its
check fails. GET /stub/reset clears all three. Any other GET answers {} (so a strict
decoder gets its defaults); any other POST is a 404. Usage: standin.py PORT [LOGFILE]
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ANSWER_SECONDS = 20
STOP_DELAY = 3

lock = threading.Lock()
chat_runs = []
stops = []
events = {}
history = []          # the one chat's stored turns, oldest first
paused_until = [0.0]  # /api/status answers 503 until this time
recovery = [""]       # /api/status db_recovery
log_path = sys.argv[2] if len(sys.argv) > 2 else None

SHAPE = {"people": "all", "detail": "full", "search": "thorough"}
CHAT = {"id": 1, "title": "Stand-in chat", "created_at": "2026-10-16 10:00:00",
        "updated_at": "2026-10-16 10:00:00", "shape": SHAPE, "turns": 0,
        "last_message_at": ""}


def note(line):
    if log_path:
        with open(log_path, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")


def answer(stopped):
    return {
        "reply": "Stopped." if stopped else "A slow answer.",
        "contacts": [], "trail": [], "truncated": stopped,
        **({"stopped": True} if stopped else {}),
        "spend": None, "question": None,
        "searched": {"contacts": 0, "conversations": 0, "messages": 0},
        "engine": "rules",
        "shape": {"requested": SHAPE, "effective": {"people": "all", "detail": None, "search": None},
                  "options": {"people": ["10", "25", "all"], "detail": ["brief", "full"],
                              "search": ["quick", "thorough"]},
                  "controls": {k: {"available": k == "people", "layer": "stand-in", "reason": "Stand-in engine."}
                               for k in ("people", "detail", "search")},
                  "people_cap": 30, "caps": None},
        "context": {"limit": 19, "turns": 0, "total": 0, "trimmed": 0, "fed_to_llm": False,
                    "scope": "conversation"},
        "conversation": {**CHAT, "turns": 2},
        "conversation_resolved": "requested",
        "cards_matched": 0, "cards_named": 0, "cards_shown": 0,
        "cards_filter": "names", "cards_filter_reasons": [],
    }


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == "/api/status":
            if time.time() < paused_until[0]:
                return self.send(503, {"error": "stand-in: paused"})
            return self.send(200, {"running": True, "mode": "local", "db_path": "", "host": "127.0.0.1",
                                   "port": self.server.server_port, "instance": "stand-in", "build": "",
                                   "ai": False, "db_recovery": recovery[0],
                                   "counts": {"total": 0, "new": 0, "described": 0, "skipped": 0, "excluded": 0}})
        if path == "/stub/pause":
            paused_until[0] = time.time() + float(query.get("seconds", ["5"])[0])
            return self.send(200, {"ok": True})
        if path == "/stub/recovery":
            recovery[0] = query.get("text", [""])[0]
            return self.send(200, {"ok": True})
        if path == "/stub/reset":
            with lock:
                history.clear()
            paused_until[0] = 0.0
            recovery[0] = ""
            return self.send(200, {"ok": True})
        if path == "/api/chats":
            return self.send(200, {"chats": [CHAT], "total": 1, "newest_id": 1, "adoption": None,
                                   "limits": {"context": 19, "transcript": 500, "title_max": 48}})
        if path in ("/api/chat/history", "/api/chat/1/history"):
            with lock:
                msgs = list(history)
            return self.send(200, {"messages": msgs, "conversation": CHAT, "conversation_id": 1,
                                   "total": len(msgs), "shown": len(msgs), "trimmed": 0, "limit": 500})
        if path == "/stub/log":
            with lock:
                return self.send(200, {"chat_runs": list(chat_runs), "stops": list(stops)})
        return self.send(200, {})

    def do_POST(self):
        path = self.path.split("?")[0]
        b = self.body()
        if path == "/api/chat":
            run = str(b.get("run_id") or "")
            ev = threading.Event()
            with lock:
                chat_runs.append(run)
                events[run] = ev
                history.append({"role": "user", "content": str(b.get("message") or "").strip(),
                                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "contacts": [],
                                "truncated": False})
            note(f"chat run={run}")
            stopped = ev.wait(ANSWER_SECONDS)
            if stopped:
                time.sleep(STOP_DELAY)
            with lock:
                events.pop(run, None)
            note(f"answer run={run} stopped={stopped}")
            return self.send(200, answer(stopped))
        if path == "/api/chat/stop":
            run = str(b.get("run_id") or "")
            with lock:
                stops.append(run)
                ev = events.get(run)
                live = len(events)
            note(f"stop run={run} known={ev is not None}")
            if ev:
                ev.set()
            return self.send(200, {"ok": True, "status": "stopping" if ev else "unknown", "live": live})
        if path == "/api/chats":
            return self.send(200, {"conversation": CHAT, "created": True})
        if path == "/api/db-recovery-notice/dismiss":
            recovery[0] = ""
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
