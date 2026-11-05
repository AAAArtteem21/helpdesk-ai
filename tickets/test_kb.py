from django.core.management import call_command
from django.test import TestCase

from .kb import build_tsquery, search_articles
from .models import Article


class KbTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_kb", verbosity=0)

    def test_seed_is_idempotent(self):
        call_command("seed_kb", verbosity=0)
        self.assertEqual(Article.objects.count(), 8)

    def test_pdf_ticket_finds_pdf_article(self):
        found = search_articles("Не можу експортувати звіт у PDF")
        self.assertEqual(found[0].slug, "export-pdf")

    def test_password_ticket_finds_reset_article(self):
        found = search_articles("Забув пароль, як відновити доступ?")
        self.assertEqual(found[0].slug, "reset-password")

    def test_no_match_and_empty_query(self):
        self.assertEqual(search_articles("абракадабра"), [])
        self.assertEqual(search_articles("?!"), [])

    def test_tsquery_contains_only_safe_tokens(self):
        self.assertEqual(build_tsquery("a'; DROP TABLE x; --"), "drop:* | tabl:*")

    def test_article_has_public_url(self):
        a = Article.objects.get(slug="export-pdf")
        self.assertTrue(a.public_url.endswith("/kb/export-pdf"))
