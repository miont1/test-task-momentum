import re

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeArticle

STOPWORDS = {
    # English stop words
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "get",
    "have",
    "help",
    "how",
    "i",
    "in",
    "is",
    "it",
    "keep",
    "keeps",
    "me",
    "my",
    "need",
    "of",
    "on",
    "or",
    "our",
    "please",
    "the",
    "this",
    "to",
    "use",
    "using",
    "we",
    "what",
    "when",
    "where",
    "why",
    "with",
    "work",
    "works",
    "working",
    "you",
    "your",
    # Ukrainian / Cyrillic stop words
    "як",
    "це",
    "що",
    "не",
    "на",
    "для",
    "під",
    "при",
    "по",
    "до",
    "чи",
    "ми",
    "ви",
    "мене",
    "мені",
    "бути",
    "але",
    "або",
    "від",
    "про",
    "тут",
    "там",
    "який",
    "які",
    "мають",
}

# Maps Ukrainian stem prefixes to English KB keywords so clean English articles
# are matched accurately by Ukrainian tickets without polluting article text.
UKRAINIAN_STEM_SYNONYMS: dict[str, list[str]] = {
    "експорт": ["export", "reports"],
    "звіт": ["reports", "export"],
    "аналітик": ["analytics", "reports"],
    "рядок": ["row"],
    "рядків": ["row"],
    "двофактор": ["two-factor", "2fa"],
    "автентифік": ["authentication", "2fa", "authenticator"],
    "безпек": ["security", "2fa"],
    "відновлен": ["recovery"],
    "рол": ["roles", "permissions"],
    "прав": ["permissions", "roles"],
    "доступ": ["permissions", "workspace"],
    "адміністратор": ["admin", "roles"],
    "команд": ["workspace", "roles"],
    "вебхук": ["webhook", "delivery"],
    "доставк": ["delivery", "webhook"],
    "таймаут": ["timeout", "webhook"],
    "повтор": ["retry", "retries"],
    "оплат": ["billing", "subscription"],
    "підписк": ["subscription", "billing"],
    "рахунк": ["invoices", "billing"],
    "рахунок": ["invoice", "billing"],
    "повернен": ["billing"],
}


def extract_keywords(query: str) -> list[str]:
    """Extract normalized, deduplicated Unicode/Cyrillic/Latin search keywords."""
    if not query or not query.strip():
        return []

    raw_tokens = re.findall(r"[a-zа-яіїєґ0-9]+", query.lower())
    keywords: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if tok not in seen:
            seen.add(tok)
            keywords.append(tok)

    for token in raw_tokens:
        if len(token) < 3 or token in STOPWORDS:
            continue
        _add(token)
        for ukr_stem, eng_terms in UKRAINIAN_STEM_SYNONYMS.items():
            if token.startswith(ukr_stem):
                for eng in eng_terms:
                    _add(eng)

    return keywords


async def search_knowledge_base(
    session: AsyncSession,
    query: str,
    limit: int = 2,
) -> list[KnowledgeArticle]:
    """Search knowledge_articles using weighted title/content ILIKE matching.

    Title matches receive weight 3 per keyword; content matches receive weight 1.
    If no valid keywords remain after filtering, returns an empty list gracefully.
    """
    keywords = extract_keywords(query)
    if not keywords:
        return []

    match_conditions = []
    score_expr = None

    for kw in keywords:
        pattern = f"%{kw}%"
        title_cond = KnowledgeArticle.title.ilike(pattern)
        content_cond = KnowledgeArticle.content.ilike(pattern)

        match_conditions.append(title_cond)
        match_conditions.append(content_cond)

        kw_score = case((title_cond, 3), else_=0) + case((content_cond, 1), else_=0)
        score_expr = kw_score if score_expr is None else (score_expr + kw_score)

    stmt = (
        select(KnowledgeArticle, score_expr.label("match_score"))
        .where(or_(*match_conditions))
        .order_by(score_expr.desc(), KnowledgeArticle.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()
    if not rows:
        return []

    top_score = rows[0][1]
    # Only include secondary articles if their relevance score is at least half of top_score
    filtered = [
        article
        for article, score in rows
        if score >= max(2, top_score // 2) or article == rows[0][0]
    ]
    return filtered
