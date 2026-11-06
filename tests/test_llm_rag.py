from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeArticle
from app.seed import INITIAL_ARTICLES
from app.services.knowledge_base import extract_keywords, search_knowledge_base


async def _seed_articles(session: AsyncSession) -> None:
    for item in INITIAL_ARTICLES:
        session.add(
            KnowledgeArticle(
                title=item["title"],
                url=item["url"],
                content=item["content"],
            )
        )
    await session.commit()


async def test_knowledge_base_search_finds_matching_articles(
    db_session: AsyncSession,
) -> None:
    await _seed_articles(db_session)

    pdf_results = await search_knowledge_base(
        db_session,
        "How can I export my analytics reports to PDF or CSV?",
    )
    assert len(pdf_results) >= 1
    assert pdf_results[0].title.startswith("Exporting Reports to PDF/CSV")
    assert pdf_results[0].url == "https://help.cloudmetrics.io/articles/export-pdf-csv"

    twofa_results = await search_knowledge_base(
        db_session,
        "Where do I enable 2FA with an authenticator app?",
    )
    assert len(twofa_results) >= 1
    assert twofa_results[0].title.startswith("Two-Factor Authentication (2FA) Setup")

    no_match_results = await search_knowledge_base(
        db_session,
        "Can I order a pepperoni pizza for lunch?",
    )
    assert no_match_results == []


async def test_knowledge_base_search_english_and_ukrainian_queries(
    db_session: AsyncSession,
) -> None:
    await _seed_articles(db_session)

    # 1. Verify Ukrainian/Cyrillic tokenization and stopword filtering
    keywords = extract_keywords("Не можу експортувати звіт у PDF для це як")
    assert "експортувати" in keywords
    assert "звіт" in keywords
    assert "pdf" in keywords
    assert "не" not in keywords
    assert "для" not in keywords
    assert "як" not in keywords

    # 2. English query ("export pdf")
    en_results = await search_knowledge_base(db_session, "export pdf")
    assert len(en_results) >= 1
    assert en_results[0].url == "https://help.cloudmetrics.io/articles/export-pdf-csv"

    # 3. Ukrainian query ("не можу експортувати звіт у pdf")
    uk_results = await search_knowledge_base(
        db_session,
        "Не можу експортувати звіт у PDF",
    )
    assert len(uk_results) >= 1
    assert uk_results[0].url == "https://help.cloudmetrics.io/articles/export-pdf-csv"

    # 4. Purely Ukrainian query without Latin terms ("не можу експортувати звіт")
    uk_only_results = await search_knowledge_base(
        db_session,
        "Не можу експортувати звіт з аналітика",
    )
    assert len(uk_only_results) >= 1
    assert uk_only_results[0].url == "https://help.cloudmetrics.io/articles/export-pdf-csv"

    # 5. Stopwords/punctuation-only query returns [] gracefully
    empty_results = await search_knowledge_base(db_session, "Як це до на ?!...")
    assert empty_results == []
