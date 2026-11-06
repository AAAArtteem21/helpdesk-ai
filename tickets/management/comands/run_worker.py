import asyncio
import logging
import signal

from django.conf import settings
from django.core.management.base import BaseCommand

from tickets.sender import Sender
from tickets.worker import Worker


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await asyncio.gather(Worker().run(stop), Sender().run(stop))


class Command(BaseCommand):
    help = "Run draft generation and reply sender"

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        mode = "real LLM" if settings.LLM_API_KEY else "keyless (static drafts)"
        logging.getLogger("worker").info("mode: %s", mode)
        asyncio.run(main())