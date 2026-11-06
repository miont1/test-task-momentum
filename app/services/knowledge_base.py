import re

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeArticle

STOPWORDS = {
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
    "we",
    "what",
    "when",
    "where",
    "why",
    "with",
    "you",
    "your",
}


def extract_keywords(query: str) -> list[str]:
    """Extract normalized, deduplicated search keywords from a user message."""
    raw_tokens = re.findall(r"[a-z0-9]+", query.lower())
    keywords: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in seen:
            seen.add(token)
            keywords.append(token)
    return keywords


async def search_knowledge_base(
    session: AsyncSession,
    query: str,
    limit: int = 2,
) -> list[KnowledgeArticle]:
    """Search knowledge_articles using weighted title/content ILIKE matching.

    Title matches receive weight 3 per keyword; content matches receive weight 1.
    Works identically across PostgreSQL and SQLite.
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
        select(KnowledgeArticle)
        .where(or_(*match_conditions))
        .order_by(score_expr.desc(), KnowledgeArticle.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
