import json
import os
import random
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAIL_RATE = float(os.environ.get("MOCK_FAIL_RATE", "0.2"))
SLOW_RATE = float(os.environ.get("MOCK_SLOW_RATE", "0.1"))
SLOW_SECONDS = float(os.environ.get("MOCK_SLOW_SECONDS", "15"))
DEDUP = os.environ.get("MOCK_DEDUP", "1") == "1"

lock = threading.Lock()
stats = {"requests": 0, "failed_5xx": 0, "slow": 0, "duplicates_suppressed": 0}
deliveries: list[dict] = []
by_key: dict[str, dict] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("mock-helpdesk:", fmt % args, flush=True)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok"})
        if self.path == "/deliveries":
            with lock:
                per_ticket = Counter(d["ticket_id"] for d in deliveries)
                return self._json(200, {
                    "stats": {**stats, "unique_keys": len(by_key),
                              "total_deliveries": len(deliveries),
                              "tickets_delivered_more_than_once":
                                  sum(1 for n in per_ticket.values() if n > 1)},
                    "deliveries": deliveries,
                })
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if self.path == "/reset":
            with lock:
                deliveries.clear()
                by_key.clear()
                for k in stats:
                    stats[k] = 0
            return self._json(200, {"status": "reset"})

        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "tickets" or parts[2] != "replies":
            return self._json(404, {"error": "not found"})
        ticket_id = parts[1]

        key = self.headers.get("Idempotency-Key")
        try:
            body = json.loads(raw).get("body")
        except (ValueError, AttributeError):
            body = None
        if not key or not isinstance(body, str) or not body:
            return self._json(400, {"error": "Idempotency-Key and body are required"})

        roll = random.random()
        with lock:
            stats["requests"] += 1
            if roll < FAIL_RATE:
                stats["failed_5xx"] += 1
                failed = True
            else:
                failed = False
        if failed:
            return self._json(503, {"error": "temporarily unavailable"})

        with lock:
            if DEDUP and key in by_key:
                stats["duplicates_suppressed"] += 1
                code, delivery = 200, by_key[key]
            else:
                delivery = {"id": str(uuid.uuid4()), "ticket_id": ticket_id,
                            "idempotency_key": key, "body": body,
                            "received_at": time.time()}
                deliveries.append(delivery)
                by_key[key] = delivery
                code = 201

        if roll < FAIL_RATE + SLOW_RATE:
            with lock:
                stats["slow"] += 1
            time.sleep(SLOW_SECONDS)
        self._json(code, {"id": delivery["id"], "status": "accepted"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    print(f"mock-helpdesk on :{port} fail={FAIL_RATE} slow={SLOW_RATE} dedup={DEDUP}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()