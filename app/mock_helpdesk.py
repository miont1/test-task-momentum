import asyncio
import random
import uuid
from collections import deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Ticket, TicketStatus, WebhookEvent
from app.schemas import MockHelpdeskReplyPayload, MockHelpdeskReplyResponse
from app.tasks import _process_ticket_draft, generate_ticket_draft

router = APIRouter(prefix="/mock-helpdesk", tags=["mock-helpdesk"])

RECEIVED_REPLIES: deque[dict[str, str]] = deque(maxlen=1000)

SAMPLE_TICKETS: list[dict[str, str]] = [
    {
        "customer_email": "olena.koval@example.ua",
        "message": "Не можу експортувати звіт у PDF за останній місяць. Який ліміт рядків?",
    },
    {
        "customer_email": "alex.chen@acme.io",
        "message": "How do I enable Two-Factor Authentication (2FA) and get recovery codes?",
    },
    {
        "customer_email": "sarah.miller@finops.com",
        "message": "Where can I download our monthly subscription billing invoices?",
    },
    {
        "customer_email": "dmytro.bondar@tech.ua",
        "message": "Які ролі та права доступу мають адміністратор та учасники команди?",
    },
    {
        "customer_email": "devops@platform.io",
        "message": "Our webhook delivery keeps failing with 5xx timeouts. How do retries work?",
    },
]


@router.post(
    "/tickets/{ticket_id}/replies",
    response_model=MockHelpdeskReplyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def receive_helpdesk_reply(
    ticket_id: str,
    payload: MockHelpdeskReplyPayload,
    simulate_error: bool = Query(default=False),
) -> MockHelpdeskReplyResponse:
    if simulate_error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Simulated Helpdesk 500 Internal Server Error",
        )

    RECEIVED_REPLIES.append(
        {
            "ticket_id": ticket_id,
            "payload_ticket_id": payload.ticket_id,
            "message": payload.message,
        }
    )
    return MockHelpdeskReplyResponse(
        status="created",
        ticket_id=ticket_id,
        message=payload.message,
    )


@router.post("/simulate-inbound", response_model=None)
async def simulate_inbound_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    sample = random.choice(SAMPLE_TICKETS)
    custom_email = sample["customer_email"]
    custom_message = sample["message"]

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            custom_email = body.get("customer_email") or custom_email
            custom_message = body.get("message") or custom_message
        except Exception:
            pass
    elif (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        try:
            form = await request.form()
            if form.get("customer_email"):
                custom_email = str(form.get("customer_email"))
            if form.get("message"):
                custom_message = str(form.get("message"))
        except Exception:
            pass

    event_id = f"evt_sim_{uuid.uuid4().hex[:8]}"
    ticket_id = f"tkt_{uuid.uuid4().hex[:6]}"
    forwarded_proto = request.headers.get("x-forwarded-proto")
    base_url = str(request.base_url).rstrip("/")
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://") :]
    reply_url = f"{base_url}/mock-helpdesk/tickets/{ticket_id}/replies"

    ticket_stmt = (
        insert(Ticket)
        .values(
            id=ticket_id,
            customer_email=custom_email,
            message=custom_message,
            reply_url=reply_url,
            status=TicketStatus.pending_draft,
        )
        .on_conflict_do_nothing(index_elements=[Ticket.id])
    )
    await db.execute(ticket_stmt)

    event_stmt = (
        insert(WebhookEvent)
        .values(
            event_id=event_id,
            ticket_id=ticket_id,
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
        .returning(WebhookEvent.event_id)
    )
    await db.execute(event_stmt)
    await db.commit()

    try:
        generate_ticket_draft.delay(ticket_id, custom_message)
    except Exception:
        asyncio.create_task(_process_ticket_draft(ticket_id, custom_message))

    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "detail": "queued",
            "event_id": event_id,
            "ticket_id": ticket_id,
            "reply_url": reply_url,
        },
    )
