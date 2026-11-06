import asyncio
import contextlib
import logging
import signal

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections

from . import jobs
from .kb import search_articles
from .llm import LLMClient, LLMError, LLMRateLimited, LLMTemporaryError

log = logging.getLogger("worker")

def _db(fn):
    """sync db func for asyncio. all cals goings in 1 connect"""
    def wrapper(*args):
        close_old_connections()
        return fn(*args)
    return sync_to_async(wrapper)

async def process(ticket, client) -> None:
    """search from DB, KB, result. """
    try:
        try:
            articles = await _db(search_articles)(ticket.message)
            draft = await client.generate_draft(ticket.message, articles)
            if not draft.text:
                raise LLMTemporaryError("empty draft")
        except LLMRateLimited as e:
            ok = await _db(jobs.finish_retry)(ticket, str(e), e.retry_after)
            log.warning("ticket %s: 429, retry later", ticket.ticket_id)
        except LLMError as e:
            if e.retryable:
                ok = await _db(jobs.finish_retry)(ticket, str(e))
                log.warning("ticket %s: %s, retry", ticket.ticket_id, e)
            else:
                ok = await _db(jobs.finish_failed)(ticket, str(e))
                log.error("ticket %s: permanent error: %s", ticket.ticket_id, e)
        except Exception as e:
            log.exception("ticket %s: unexpected error", ticket.ticket_id)
            ok = await _db(jobs.finish_retry)(ticket, f"unexpected: {e!r}")
        else:
            ok = await _db(jobs.finish_ok)(ticket, draft.text, draft.sources)
            if ok:
                log.info("ticket %s: draft ready (sources=%s)", ticket.ticket_id, draft.sources)
        if not ok:
            log.warning("ticket %s: lease lost, result discarded", ticket.ticket_id)
    except Exception:
        log.exception("ticket %s: failed to persist result", ticket.ticket_id)


class Worker:
    def __init__(self, client = None, concurrency=None, poll=None):
        self.client = client
        self.concurrency = concurrency or settings.LLM_CONCURRENCY
        self.poll = poll or settings.WORKER_POLL_SECONDS
        self.stop = None

    async def run(self, stop=None) -> None:
        self.stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)

        client = self.client or LLMClient()
        inflight: set[asyncio.Task] = set()
        log.info("worker started (concurrency=%s)", self.concurrency)

        while not self.stop.is_set():
            claimed=[]
            free = self.concurrency - len(inflight)
            if free > 0:
                try:
                    claimed = await _db(jobs.claim)(free)
                except Exception:
                    log.exception("claim failed")
            for t in claimed:
                task = asyncio.create_task(process(t, client))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            if claimed:
                continue
            if inflight:
                await asyncio.wait(inflight, timeout=self.poll,
                                   return_when=asyncio.FIRST_COMPLETED)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), self.poll)

        log.info("stopping, waiting for %d in-flight tickets", len(inflight))
        if inflight:
            await asyncio.wait(inflight, timeout=45)
        await client.aclose()
