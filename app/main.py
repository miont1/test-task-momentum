from fastapi import Depends, FastAPI, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Ticket, TicketStatus, WebhookEvent
from app.schemas import HealthResponse, WebhookPayload, WebhookResponse
from app.tasks import generate_ticket_draft

app = FastAPI(
    title="AI-First Helpdesk Assistant",
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/webhooks/helpdesk",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
)
async def ingest_helpdesk_webhook(
    payload: WebhookPayload,
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    # 1. Upsert parent Ticket first (atomic ON CONFLICT DO NOTHING)
    ticket_stmt = (
        insert(Ticket)
        .values(
            id=payload.ticket_id,
            customer_email=str(payload.customer_email),
            message=payload.message,
            reply_url=str(payload.reply_url),
            status=TicketStatus.pending_draft,
        )
        .on_conflict_do_nothing(index_elements=[Ticket.id])
    )
    await db.execute(ticket_stmt)

    # 2. Atomic insert of WebhookEvent with RETURNING event_id
    event_stmt = (
        insert(WebhookEvent)
        .values(
            event_id=payload.event_id,
            ticket_id=payload.ticket_id,
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
        .returning(WebhookEvent.event_id)
    )
    result = await db.execute(event_stmt)
    inserted_event_id = result.scalar_one_or_none()
    await db.commit()

    # 3. Deduplication check
    if inserted_event_id is None:
        return WebhookResponse(status="ok", detail="duplicate_ignored")

    # 4. Enqueue background task to Redis via Celery
    generate_ticket_draft.delay(payload.ticket_id, payload.message)
    return WebhookResponse(status="ok", detail="queued")
