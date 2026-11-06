from unittest.mock import MagicMock

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket, TicketStatus, WebhookEvent


async def test_webhook_ingestion_success(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_celery_delay: MagicMock,
) -> None:
    payload = {
        "event_id": "evt_1001",
        "ticket_id": "tkt_501",
        "customer_email": "alice@example.com",
        "message": "How do I export my analytics reports to CSV?",
        "reply_url": "https://helpdesk.example.com/api/tickets/tkt_501/reply",
    }

    response = await client.post("/webhooks/helpdesk", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "detail": "queued"}

    mock_celery_delay.assert_called_once_with(
        payload["ticket_id"],
        payload["message"],
    )

    result = await db_session.execute(
        select(Ticket).where(Ticket.id == payload["ticket_id"])
    )
    ticket = result.scalar_one_or_none()
    assert ticket is not None
    assert ticket.id == "tkt_501"
    assert ticket.customer_email == "alice@example.com"
    assert ticket.message == payload["message"]
    assert ticket.status == TicketStatus.pending_draft


async def test_webhook_deduplication_duplicate_event_id(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_celery_delay: MagicMock,
) -> None:
    payload = {
        "event_id": "evt_dup_2001",
        "ticket_id": "tkt_502",
        "customer_email": "bob@example.com",
        "message": "Need help setting up Two-Factor Authentication.",
        "reply_url": "https://helpdesk.example.com/api/tickets/tkt_502/reply",
    }

    first_response = await client.post("/webhooks/helpdesk", json=payload)
    assert first_response.status_code == 200
    assert first_response.json() == {"status": "ok", "detail": "queued"}

    second_response = await client.post("/webhooks/helpdesk", json=payload)
    assert second_response.status_code == 200
    assert second_response.json() == {"status": "ok", "detail": "duplicate_ignored"}

    mock_celery_delay.assert_called_once_with(
        payload["ticket_id"],
        payload["message"],
    )

    events_result = await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.event_id == payload["event_id"])
    )
    assert len(events_result.scalars().all()) == 1


async def test_different_events_for_same_ticket(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_celery_delay: MagicMock,
) -> None:
    first_payload = {
        "event_id": "evt_3001",
        "ticket_id": "tkt_503",
        "customer_email": "charlie@example.com",
        "message": "Initial message about billing.",
        "reply_url": "https://helpdesk.example.com/api/tickets/tkt_503/reply",
    }
    second_payload = {
        "event_id": "evt_3002",
        "ticket_id": "tkt_503",
        "customer_email": "charlie@example.com",
        "message": "Follow-up message on the same ticket.",
        "reply_url": "https://helpdesk.example.com/api/tickets/tkt_503/reply",
    }

    first_res = await client.post("/webhooks/helpdesk", json=first_payload)
    assert first_res.status_code == 200
    assert first_res.json() == {"status": "ok", "detail": "queued"}

    second_res = await client.post("/webhooks/helpdesk", json=second_payload)
    assert second_res.status_code == 200
    assert second_res.json() == {"status": "ok", "detail": "queued"}

    assert mock_celery_delay.call_count == 2

    tickets_result = await db_session.execute(
        select(Ticket).where(Ticket.id == "tkt_503")
    )
    assert len(tickets_result.scalars().all()) == 1

    events_result = await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.ticket_id == "tkt_503")
    )
    assert len(events_result.scalars().all()) == 2


async def test_webhook_payload_validation_failure(
    client: AsyncClient,
    mock_celery_delay: MagicMock,
) -> None:
    invalid_email_payload = {
        "event_id": "evt_4001",
        "ticket_id": "tkt_504",
        "customer_email": "not-an-email",
        "message": "Invalid email test",
        "reply_url": "https://helpdesk.example.com/api/tickets/tkt_504/reply",
    }
    res_invalid_email = await client.post(
        "/webhooks/helpdesk", json=invalid_email_payload
    )
    assert res_invalid_email.status_code == 422

    missing_reply_url_payload = {
        "event_id": "evt_4002",
        "ticket_id": "tkt_505",
        "customer_email": "valid@example.com",
        "message": "Missing reply_url test",
    }
    res_missing_url = await client.post(
        "/webhooks/helpdesk", json=missing_reply_url_payload
    )
    assert res_missing_url.status_code == 422

    mock_celery_delay.assert_not_called()
