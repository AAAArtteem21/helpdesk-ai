import json
from pathlib import Path
from django.core.management.base import BaseCommand
from tickets.models import Article

class Command(BaseCommand):
    help = "Load KB articles from tickets/kb/articles.json (Idempotent)"

    def handle(self, *args, **options):
        path = Path(__file__).resolve().parents[2] / "kb" / "articles.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        for item in items:
            Article.objects.update_or_create(
                slug=item["slug"],
                defaults={"title": item["title"], "body": item["body"]},
            )
        self.stdout.write(f"KB: {len(items)} articles loaded")
