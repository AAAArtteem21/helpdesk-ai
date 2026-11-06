import asyncio
import contextlib
import logging

import httpx
from django.conf import settings

from . import send_jobs as sj
from .worker import _db

log = logging.getLogger("sender")


def _retry_after(r: httpx.Response) -> float:
    try:
        return min(max(float(r.headers.get("retry-after", 5)), 1.0), 60.0)
    except ValueError:
        return 5.0


async def deliver(ticket, http) -> None:
    """1 try response. never drop open"""
    try:
        try:
            r = await http.post(
                ticket.reply_url,
                headers={"Idempotency-Key": str(ticket.reply_key)},
                json={"ticket_id": ticket.ticket_id, "body": ticket.draft},
            )
        except httpx.InvalidURL as e:
            ok = await _db(sj.finish_failed)(ticket, f"bad reply_url: {e}")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            ok = await _db(sj.finish_retry)(ticket, f"outcome unknown: {e!r}")
            log.warning("ticket %s: %r, will retry with the same key", ticket.ticket_id, e)
        except Exception as e:
            log.exception("ticket %s: unexpected error", ticket.ticket_id)
            ok = await _db(sj.finish_retry)(ticket, f"unexpected: {e!r}")
        else:
            code = r.status_code
            if 200 <= code < 300:
                ok = await _db(sj.finish_sent)(ticket)
                log.info("ticket %s: sent (http %s, attempt %s)", ticket.ticket_id, code, ticket.attempts)
            elif code == 429:
                ok = await _db(sj.finish_retry)(ticket, "helpdesk 429", _retry_after(r))
            elif code == 408 or code >= 500:
                ok = await _db(sj.finish_retry)(ticket, f"helpdesk {code}")
                log.warning("ticket %s: helpdesk %s, will retry", ticket.ticket_id, code)
            else:
                ok = await _db(sj.finish_failed)(ticket, f"helpdesk {code}: {r.text[:200]}")
                log.error("ticket %s: permanent error %s", ticket.ticket_id, code)
        if not ok:
            log.warning("ticket %s: lease lost, result discarded", ticket.ticket_id)
    except Exception:
        log.exception("ticket %s: failed to persist result", ticket.ticket_id)


class Sender:
    def __init__(self, concurrency=None, poll=None, transport=None):
        self.concurrency = concurrency or settings.SEND_CONCURRENCY
        self.poll = poll or settings.WORKER_POLL_SECONDS
        self.transport = transport

    async def run(self, stop: asyncio.Event) -> None:
        http = httpx.AsyncClient(timeout=settings.SEND_TIMEOUT, transport=self.transport)
        inflight: set[asyncio.Task] = set()
        log.info("sender started (concurrency=%s)", self.concurrency)

        while not stop.is_set():
            claimed = []
            free = self.concurrency - len(inflight)
            if free > 0:
                try:
                    claimed = await _db(sj.claim)(free)
                except Exception:
                    log.exception("claim failed")
            for t in claimed:
                task = asyncio.create_task(deliver(t, http))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            if claimed:
                continue
            if inflight:
                await asyncio.wait(inflight, timeout=self.poll,
                                   return_when=asyncio.FIRST_COMPLETED)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.poll)

        if inflight:
            await asyncio.wait(inflight, timeout=45)
        await http.aclose()