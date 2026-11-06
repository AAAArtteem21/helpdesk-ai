import re

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F

from .models import Article

STOPWORDS = {
    "можу", "можна", "мені", "мене", "при", "для", "про", "щоб", "але", "або",
    "які", "який", "яка", "ваш", "ваша", "наш", "мій", "моя", "будь", "ласка",
    "добрий", "день", "дякую", "hello", "the",
}


def _stem(token: str) -> str:
    return token[: max(4, min(len(token) - 2, 6))]


def build_tsquery(text: str) -> str:
    stems = []
    for token in re.findall(r"[^\W_]+", text.lower()):
        if len(token) < 3 or token in STOPWORDS:
            continue
        stem = _stem(token)
        if stem not in stems:
            stems.append(stem)
    return " | ".join(f"{s}:*" for s in stems)


def search_articles(text: str, limit: int = 3) -> list[Article]:
    raw = build_tsquery(text)
    if not raw:
        return []
    query = SearchQuery(raw, search_type="raw", config="simple")
    return list(
        Article.objects.filter(search_vector=query)
        .annotate(rank=SearchRank(F("search_vector"), query))
        .order_by("-rank")[:limit]
    )