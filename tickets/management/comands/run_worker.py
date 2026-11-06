import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from tickets.worker import Worker

class Command(BaseCommand):
    help = "Run the draft-generation worker"

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        mode = "real LLM" if settings.LLM_API_KEY else "keyless (static drafts)"
        logging.getLogger("worker").info("mode: %s", mode)
        asyncio.run(Worker().run())
