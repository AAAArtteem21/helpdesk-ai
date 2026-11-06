#!/usr/bin/env python3
"""End-to-end simulation: duplicates, parallel duplicates, load, delivery check.

    python simulate.py                       # 500 tickets over 10 minutes
    python simulate.py --count 60 --duration 60   # quick run
"""
import argparse
import http.cookiejar
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

TERMINAL = ("sent", "failed", "rejected")
results = []


def check(name, ok, details=""):
    results.append((name, ok, details))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {details}")


def post_webhook(base, payload):
    req = urllib.request.Request(
        f"{base}/webhooks/helpdesk", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
            return r.status, body.get("status"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, None, time.monotonic() - t0
    except Exception:
        return 0, None, time.monotonic() - t0


def payload(args, run, event_id, ticket_id):
    return {
        "event_id": event_id, "ticket_id": ticket_id,
        "customer_email": "sim@example.com",
        "message": "Не можу експортувати звіт у PDF, кнопка неактивна",
        "reply_url": f"{args.reply_base}/tickets/{ticket_id}/replies",
    }


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def stats(args, run):
    q = urllib.parse.quote(run)
    return get_json(f"{args.base}/api/stats?prefix={q}")


def approver(args, run, stop, counter):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def csrf():
        return next((c.value for c in jar if c.name == "csrftoken"), None)

    while not stop.is_set():
        try:
            ready = stats(args, run)["draft_ready"]
            for item in ready:
                if csrf() is None:
                    opener.open(f"{args.base}/tickets/{item['id']}", timeout=10).read()
                data = urllib.parse.urlencode({"draft": item["draft"]}).encode()
                req = urllib.request.Request(
                    f"{args.base}/tickets/{item['id']}/approve", data=data,
                    headers={"X-CSRFToken": csrf() or "", "Referer": args.base + "/"})
                opener.open(req, timeout=10).read()
                counter[0] += 1
        except Exception as e:
            print("  approver:", repr(e))
        stop.wait(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--mock", default="http://localhost:9000", help="mock helpdesk, view from host")
    ap.add_argument("--reply-base", default="http://mock-helpdesk:9000",
                    help="mock helpdesk as seen from inside docker")
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--duration", type=float, default=600, help="seconds to spread the load over")
    ap.add_argument("--timeout", type=float, default=900, help="max wait after the last webhook")
    args = ap.parse_args()

    run = "sim" + uuid.uuid4().hex[:6]
    print(f"run id: {run}")
    urllib.request.urlopen(urllib.request.Request(f"{args.mock}/reset", method="POST"), timeout=5)

    print("A. sequential duplicates")
    outs = [post_webhook(args.base, payload(args, run, f"{run}-dupA", f"{run}-A"))[:2]
            for _ in range(5)]
    check("all 200", all(s == 200 for s, _ in outs))
    check("1 accepted, 4 duplicate",
          [b for _, b in outs].count("accepted") == 1 and [b for _, b in outs].count("duplicate") == 4,
          str([b for _, b in outs]))

    print("B. 30 parallel identical event_id")
    barrier = threading.Barrier(30)

    def racer(_):
        barrier.wait()
        return post_webhook(args.base, payload(args, run, f"{run}-dupB", f"{run}-B"))[:2]

    with ThreadPoolExecutor(30) as ex:
        outs = list(ex.map(racer, range(30)))
    bodies = [b for _, b in outs]
    check("all 200", all(s == 200 for s, _ in outs))
    check("exactly 1 accepted", bodies.count("accepted") == 1, f"accepted={bodies.count('accepted')}")

    print(f"C. load: {args.count} tickets over {args.duration:.0f}s")
    stop = threading.Event()
    approved = [0]
    threading.Thread(target=approver, args=(args, run, stop, approved), daemon=True).start()

    latencies, bad = [], 0
    lock = threading.Lock()
    interval = args.duration / args.count
    start = time.monotonic()

    def fire(i):
        nonlocal bad
        s, body, dt = post_webhook(args.base, payload(args, run, f"{run}-e{i}", f"{run}-{i}"))
        with lock:
            latencies.append(dt)
            if s != 200 or body != "accepted":
                bad += 1

    with ThreadPoolExecutor(50) as ex:
        for i in range(args.count):
            delay = start + i * interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            ex.submit(fire, i)
            if i and i % 50 == 0:
                print(f"  sent {i}/{args.count}")
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    check("all webhooks accepted", bad == 0, f"bad={bad}")
    check("webhook p95 < 2s", p95 < 2, f"p50={statistics.median(latencies)*1000:.0f}ms "
          f"p95={p95*1000:.0f}ms max={latencies[-1]*1000:.0f}ms")

    print("E. waiting for processing to finish")
    expected = args.count + 2
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        s = stats(args, run)
        done = sum(s["counts"].get(k, 0) for k in TERMINAL)
        print(f"  {s['counts']}")
        if s["total"] == expected and done == expected:
            break
        time.sleep(5)
    stop.set()
    s = stats(args, run)
    c = s["counts"]
    check("no tickets lost", s["total"] == expected, f"{s['total']}/{expected}")
    check("everything reached a final state",
          sum(c.get(k, 0) for k in TERMINAL) == expected, str(c))

    dl = get_json(f"{args.mock}/deliveries")
    mine = [d for d in dl["deliveries"] if d["ticket_id"].startswith(run)]
    per_ticket = {}
    for d in mine:
        per_ticket[d["ticket_id"]] = per_ticket.get(d["ticket_id"], 0) + 1
    twice = {t: n for t, n in per_ticket.items() if n > 1}
    check("no ticket delivered twice to the customer", not twice, str(twice) if twice else "")
    check("every 'sent' ticket really was delivered", len(per_ticket) >= c.get("sent", 0),
          f"sent={c.get('sent', 0)} delivered={len(per_ticket)}")
    st = dl["stats"]
    print(f"\n  mock stats: requests={st['requests']} 5xx={st['failed_5xx']} "
          f"slow={st['slow']} duplicates_suppressed={st['duplicates_suppressed']}")
    print(f"  approved by simulated operator: {approved[0]}, failed: {c.get('failed', 0)}")
    if c.get("failed"):
        print("  note: 'failed' after retries means delivery outcome is unknown; "
              "the mock may still hold the reply (counted above).")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()