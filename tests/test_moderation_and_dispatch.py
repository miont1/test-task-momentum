import asyncio
from unittest.mock import AsyncMock

import httpx
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket, TicketStatus


async def test_approve_flow_triggers_dispatch_and_marks_ticket_sent(
    client: AsyncClient,
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    ticket = Ticket(
        id="tkt_approve_01",
        customer_email="operator.test@example.com",
        message="How do I export my report to CSV?",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_approve_01/replies",
        draft_reply="Original AI draft reply.",
        status=TicketStatus.draft_ready,
    )
    db_session.add(ticket)
    await db_session.commit()

    mock_http_post = mocker.patch(
        "app.services.dispatch._send_webhook_request",
        new_callable=AsyncMock,
        return_value=httpx.Response(201, json={"status": "created"}),
    )

    response = await client.post(
        "/tickets/tkt_approve_01/approve",
        json={"draft_reply": "Edited operator reply with citation."},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ticket_status"] == TicketStatus.sent.value

    mock_http_post.assert_awaited_once_with(
        "https://helpdesk.example.com/api/tickets/tkt_approve_01/replies",
        {
            "ticket_id": "tkt_approve_01",
            "message": "Edited operator reply with citation.",
        },
    )

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.sent
    assert ticket.draft_reply == "Edited operator reply with citation."


async def test_single_delivery_protection_prevents_double_http_requests(
    client: AsyncClient,
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    ticket = Ticket(
        id="tkt_parallel_02",
        customer_email="parallel@example.com",
        message="Duplicate click test",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_parallel_02/replies",
        draft_reply="Single delivery draft reply.",
        status=TicketStatus.draft_ready,
    )
    db_session.add(ticket)
    await db_session.commit()

    async def _slow_post(reply_url: str, payload: dict[str, str]) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(201, json={"status": "created"})

    mock_http_post = mocker.patch(
        "app.services.dispatch._send_webhook_request",
        new_callable=AsyncMock,
        side_effect=_slow_post,
    )

    res1, res2 = await asyncio.gather(
        client.post(
            "/tickets/tkt_parallel_02/approve",
            headers={"Accept": "application/json"},
        ),
        client.post(
            "/tickets/tkt_parallel_02/approve",
            headers={"Accept": "application/json"},
        ),
    )

    assert res1.status_code == 200
    assert res2.status_code == 200
    statuses = {res1.json()["status"], res2.json()["status"]}
    assert statuses == {"ok", "ignored"}

    # Strictly 1 outbound HTTP call despite 2 parallel approve requests
    assert mock_http_post.await_count == 1

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.sent


async def test_dispatch_retries_on_500_and_marks_failed_when_exhausted(
    client: AsyncClient,
    db_session: AsyncSession,
    mocker: MockerFixture,
) -> None:
    ticket = Ticket(
        id="tkt_retry_fail_03",
        customer_email="retry@example.com",
        message="Retry exhaustion test",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_retry_fail_03/replies",
        draft_reply="Draft for retry test.",
        status=TicketStatus.draft_ready,
    )
    db_session.add(ticket)
    await db_session.commit()

    mock_http_post = mocker.patch(
        "app.services.dispatch._send_webhook_request",
        new_callable=AsyncMock,
        return_value=httpx.Response(500, json={"detail": "Internal Server Error"}),
    )
    mocker.patch("app.services.dispatch.asyncio.sleep", new_callable=AsyncMock)

    response = await client.post(
        "/tickets/tkt_retry_fail_03/approve",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["ticket_status"] == TicketStatus.failed.value
    assert mock_http_post.await_count == 3

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.failed


async def test_reject_flow_transitions_ticket_to_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    ticket = Ticket(
        id="tkt_reject_04",
        customer_email="reject@example.com",
        message="Spam or irrelevant ticket",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_reject_04/replies",
        draft_reply="Draft that will be rejected.",
        status=TicketStatus.draft_ready,
    )
    db_session.add(ticket)
    await db_session.commit()

    response = await client.post(
        "/tickets/tkt_reject_04/reject",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "ticket_id": "tkt_reject_04",
        "ticket_status": TicketStatus.rejected.value,
        "detail": "rejected",
    }

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.rejected


async def test_reject_flow_guards_against_rejecting_sent_ticket(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    ticket = Ticket(
        id="tkt_already_sent_05",
        customer_email="sent@example.com",
        message="Already sent ticket",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_already_sent_05/replies",
        draft_reply="Sent reply.",
        status=TicketStatus.sent,
    )
    db_session.add(ticket)
    await db_session.commit()

    response = await client.post(
        "/tickets/tkt_already_sent_05/reject",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "ticket_id": "tkt_already_sent_05",
        "ticket_status": TicketStatus.sent.value,
        "detail": "cannot_reject_in_current_state",
    }

    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.sent


async def test_operator_dashboard_renders_200_with_tickets(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    ticket = Ticket(
        id="tkt_ui_500",
        customer_email="ui.viewer@example.com",
        message="Checking operator web dashboard rendering.",
        reply_url="https://helpdesk.example.com/api/tickets/tkt_ui_500/replies",
        draft_reply="Prepared AI draft for dashboard.",
        status=TicketStatus.draft_ready,
    )
    db_session.add(ticket)
    await db_session.commit()

    response = await client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "tkt_ui_500" in html
    assert "ui.viewer@example.com" in html
    assert "Checking operator web dashboard rendering." in html
    assert "Prepared AI draft for dashboard." in html
    assert "draft_ready" in html


async def test_mock_helpdesk_endpoints(
    client: AsyncClient,
    mock_celery_delay,
) -> None:
    # 1. Outbound mock receiver returns 201 Created
    ok_res = await client.post(
        "/mock-helpdesk/tickets/tkt_mock_1/replies",
        json={"ticket_id": "tkt_mock_1", "message": "Hello from AI assistant"},
    )
    assert ok_res.status_code == 201
    assert ok_res.json()["status"] == "created"

    # 2. Outbound mock receiver with ?simulate_error=true returns 500
    err_res = await client.post(
        "/mock-helpdesk/tickets/tkt_mock_1/replies?simulate_error=true",
        json={"ticket_id": "tkt_mock_1", "message": "Hello from AI assistant"},
    )
    assert err_res.status_code == 500

    # 3. Simulate inbound helper creates ticket and queues draft task
    sim_res = await client.post(
        "/mock-helpdesk/simulate-inbound",
        headers={"Accept": "application/json"},
    )
    assert sim_res.status_code == 200
    assert sim_res.json()["detail"] == "queued"
    mock_celery_delay.assert_called_once()
