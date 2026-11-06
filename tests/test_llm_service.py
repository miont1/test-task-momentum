from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pytest_mock import MockerFixture
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import KnowledgeArticle, Ticket, TicketStatus
from app.seed import INITIAL_ARTICLES
from app.services.knowledge_base import search_knowledge_base
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


async def test_llm_mock_mode_simulates_delay_and_cites_article_english(
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
    assert "Reference Documentation:" in draft
    assert articles[0].title in draft
    assert articles[0].url in draft


async def test_english_webhook_ticket_does_not_contain_cyrillic_or_unrelated_articles(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    await _seed_articles(db_session)
    msg = "Our webhook delivery keeps failing with 5xx timeouts. How do retries work?"
    articles = await search_knowledge_base(db_session, msg, limit=2)
    assert len(articles) == 1
    assert articles[0].title == "Webhook Delivery Failures & Retries"

    mocker.patch("app.services.llm.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)

    draft = await generate_draft_reply(customer_message=msg, articles=articles)
    assert "Webhook Delivery Failures & Retries" in draft
    assert "https://help.cloudmetrics.io/articles/webhooks-retry" in draft
    assert "roles-permissions" not in draft
    assert "Вебхуки" not in draft


async def test_llm_mock_mode_ukrainian_query_formats_ukrainian_draft_with_citations(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    await _seed_articles(db_session)
    articles = await search_knowledge_base(
        db_session,
        "Не можу експортувати звіт у PDF",
        limit=1,
    )
    assert len(articles) == 1

    mocker.patch("app.services.llm.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)

    draft = await generate_draft_reply(
        customer_message="Не можу експортувати звіт у PDF",
        articles=articles,
    )

    assert "Вітаємо!" in draft
    assert "Корисні посилання:" in draft
    assert articles[0].title in draft
    assert "https://help.cloudmetrics.io/articles/export-pdf-csv" in draft


async def test_llm_mock_mode_fallback_when_no_articles_match(
    mocker: MockerFixture,
) -> None:
    mocker.patch("app.services.llm.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)

    en_draft = await generate_draft_reply(
        customer_message="Unrelated question with zero KB matches",
        articles=[],
    )
    assert "CloudMetrics Support" in en_draft
    assert "https://help.cloudmetrics.io" not in en_draft

    uk_draft = await generate_draft_reply(
        customer_message="Питання без жодних збігів у базі",
        articles=[],
    )
    assert "Служба підтримки CloudMetrics" in uk_draft
    assert "https://help.cloudmetrics.io" not in uk_draft


async def test_llm_live_api_mode_calls_openai_with_article_context(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    await _seed_articles(db_session)
    articles = await search_knowledge_base(db_session, "enable 2FA", limit=1)
    assert len(articles) == 1

    mocker.patch("app.services.llm.acquire_llm_rate_limit", new_callable=AsyncMock)
    mocker.patch.object(settings, "openai_api_key", "sk-test-live-key")

    expected_reply = (
        "You can enable 2FA under Settings > Security. "
        "See: https://help.cloudmetrics.io/articles/2fa-setup"
    )
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=expected_reply))]
    )
    mock_create = AsyncMock(return_value=fake_response)
    mock_client_instance = mocker.MagicMock()
    mock_client_instance.chat.completions.create = mock_create
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)
    mock_openai_cls = mocker.patch(
        "app.services.llm.AsyncOpenAI",
        return_value=mock_client_instance,
    )

    draft = await generate_draft_reply(
        customer_message="How do I enable 2FA?",
        articles=articles,
    )

    mock_openai_cls.assert_called_once_with(api_key="sk-test-live-key")
    mock_create.assert_awaited_once()
    assert draft == expected_reply


async def test_task_transitions_ticket_from_pending_to_draft_ready(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    await _seed_articles(db_session)

    ticket = Ticket(
        id="tkt_rag_101",
        customer_email="customer@example.com",
        message="Не можу експортувати звіт у PDF",
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


async def test_task_does_not_overwrite_concurrently_rejected_ticket(
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    ticket = Ticket(
        id="tkt_rejected_race_103",
        customer_email="customer@example.com",
        message="Export reports to PDF",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_rejected_race_103/reply",
        status=TicketStatus.pending_draft,
    )
    db_session.add(ticket)
    await db_session.commit()

    async def _reject_during_llm(*_args, **_kwargs) -> str:
        ticket.status = TicketStatus.rejected
        await db_session.commit()
        return "Draft generated after operator already rejected."

    mocker.patch(
        "app.tasks.generate_draft_reply",
        new_callable=AsyncMock,
        side_effect=_reject_during_llm,
    )

    final_status = await _process_ticket_draft(
        ticket_id="tkt_rejected_race_103",
        message=ticket.message,
    )
    assert final_status == TicketStatus.rejected.value

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.rejected
    assert ticket.draft_reply is None

