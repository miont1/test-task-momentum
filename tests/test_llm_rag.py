from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from pytest_mock import MockerFixture
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeArticle, Ticket, TicketStatus
from app.seed import INITIAL_ARTICLES
from app.services.knowledge_base import extract_keywords, search_knowledge_base
from app.services.llm import generate_draft_reply
from app.tasks import _process_ticket_draft, generate_ticket_draft


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


async def test_llm_mock_mode_simulates_delay_and_cites_article(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    await _seed_articles(db_session)
    articles = await search_knowledge_base(db_session, "Export report to PDF", limit=1)
    assert len(articles) == 1

    mock_sleep = mocker.patch("app.services.llm.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)

    draft = await generate_draft_reply(
        customer_message="How do I export a report to PDF?",
        articles=articles,
    )

    mock_sleep.assert_awaited_once()
    delay_arg = mock_sleep.call_args.args[0]
    assert 5.0 <= delay_arg <= 30.0
    assert articles[0].title in draft
    assert articles[0].url in draft


async def test_llm_mock_mode_fallback_when_no_articles_match(
    mocker: MockerFixture,
) -> None:
    mocker.patch("app.services.llm.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)

    draft = await generate_draft_reply(
        customer_message="Unrelated question with zero KB matches",
        articles=[],
    )
    assert "CloudMetrics Support" in draft
    assert "https://help.cloudmetrics.io" not in draft


async def test_task_transitions_ticket_from_pending_to_draft_ready(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    await _seed_articles(db_session)

    ticket = Ticket(
        id="tkt_rag_101",
        customer_email="customer@example.com",
        message="I need help exporting my analytics reports to PDF.",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_rag_101/reply",
        status=TicketStatus.pending_draft,
    )
    db_session.add(ticket)
    await db_session.commit()

    @asynccontextmanager
    async def _override_session_local():
        yield db_session

    mocker.patch("app.tasks.AsyncSessionLocal", side_effect=_override_session_local)
    mocker.patch("app.services.llm.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)

    final_status = await _process_ticket_draft(
        ticket_id="tkt_rag_101",
        message=ticket.message,
    )
    assert final_status == TicketStatus.draft_ready.value

    result = await db_session.execute(
        select(Ticket).where(Ticket.id == "tkt_rag_101")
    )
    updated_ticket = result.scalar_one()
    assert updated_ticket.status == TicketStatus.draft_ready
    assert updated_ticket.draft_reply is not None
    assert "https://help.cloudmetrics.io/articles/export-pdf-csv" in updated_ticket.draft_reply


async def test_task_marks_ticket_failed_on_unexpected_error(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    ticket = Ticket(
        id="tkt_rag_fail_102",
        customer_email="customer@example.com",
        message="Billing invoice question",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_rag_fail_102/reply",
        status=TicketStatus.pending_draft,
    )
    db_session.add(ticket)
    await db_session.commit()

    @asynccontextmanager
    async def _override_session_local():
        yield db_session

    mocker.patch("app.tasks.AsyncSessionLocal", side_effect=_override_session_local)
    mocker.patch(
        "app.tasks.generate_draft_reply",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Simulated LLM outage"),
    )

    final_status = await _process_ticket_draft(
        ticket_id="tkt_rag_fail_102",
        message=ticket.message,
    )
    assert final_status == TicketStatus.failed.value

    result = await db_session.execute(
        select(Ticket).where(Ticket.id == "tkt_rag_fail_102")
    )
    updated_ticket = result.scalar_one()
    assert updated_ticket.status == TicketStatus.failed
    assert generate_ticket_draft.rate_limit == "60/m"
