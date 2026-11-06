from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.mock_helpdesk import router as mock_helpdesk_router
from app.models import Ticket, TicketStatus, WebhookEvent
from app.schemas import (
    HealthResponse,
    TicketActionResponse,
    WebhookPayload,
    WebhookResponse,
)
from app.services.dispatch import dispatch_ticket_reply
from app.tasks import generate_ticket_draft

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(
    title="AI-First Helpdesk Assistant",
    version="0.1.0",
)

app.include_router(mock_helpdesk_router)


def _wants_html_redirect(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    if "application/json" in accept:
        return False
    if "text/html" in accept or "application/x-www-form-urlencoded" in content_type:
        return True
    return False


async def _extract_edited_draft(request: Request) -> str | None:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("draft_reply") is not None:
                return str(body["draft_reply"])
        except Exception:
            return None
    elif (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        try:
            form = await request.form()
            if form.get("draft_reply") is not None:
                return str(form.get("draft_reply"))
        except Exception:
            return None
    return None


@app.get("/health", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/", response_class=HTMLResponse)
async def operator_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(
        select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())
    )
    tickets = list(result.scalars().all())
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"tickets": tickets},
    )


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


@app.post("/tickets/{ticket_id}/approve", response_model=None)
async def approve_ticket(
    ticket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    existing_res = await db.execute(
        select(Ticket.id, Ticket.draft_reply, Ticket.status).where(
            Ticket.id == ticket_id
        )
    )
    existing_row = existing_res.one_or_none()
    if existing_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    _, current_draft, _ = existing_row
    edited_draft = await _extract_edited_draft(request)
    effective_draft = (
        edited_draft.strip()
        if (edited_draft is not None and edited_draft.strip())
        else current_draft
    )

    stmt = (
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.status == TicketStatus.draft_ready,
        )
        .values(
            draft_reply=effective_draft,
            status=TicketStatus.sending,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Ticket.id, Ticket.reply_url, Ticket.draft_reply)
    )
    lock_res = await db.execute(stmt)
    locked_row = lock_res.one_or_none()

    if locked_row is None:
        await db.rollback()
        if _wants_html_redirect(request):
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

        cur_res = await db.execute(
            select(Ticket.status).where(Ticket.id == ticket_id)
        )
        current_status = cur_res.scalar_one()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=TicketActionResponse(
                status="ignored",
                ticket_id=ticket_id,
                ticket_status=current_status.value,
                detail="duplicate_or_not_ready",
            ).model_dump(),
        )

    # Commit the atomic draft edit + 'sending' state transition before network I/O
    await db.commit()

    dispatch_result = await dispatch_ticket_reply(
        ticket_id=ticket_id,
        already_locked=True,
        base_backoff=0.05,
    )

    if _wants_html_redirect(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=TicketActionResponse(
            status="ok",
            ticket_id=ticket_id,
            ticket_status=dispatch_result,
            detail="dispatched",
        ).model_dump(),
    )


@app.post("/tickets/{ticket_id}/reject", response_model=None)
async def reject_ticket(
    ticket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    exists_res = await db.execute(
        select(Ticket.status).where(Ticket.id == ticket_id)
    )
    current_status = exists_res.scalar_one_or_none()
    if current_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    stmt = (
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.status.in_([TicketStatus.pending_draft, TicketStatus.draft_ready]),
        )
        .values(
            status=TicketStatus.rejected,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Ticket.id)
    )
    result = await db.execute(stmt)
    updated_id = result.scalar_one_or_none()

    if updated_id is None:
        await db.rollback()
        if _wants_html_redirect(request):
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=TicketActionResponse(
                status="ignored",
                ticket_id=ticket_id,
                ticket_status=current_status.value,
                detail="cannot_reject_in_current_state",
            ).model_dump(),
        )

    await db.commit()

    if _wants_html_redirect(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=TicketActionResponse(
            status="ok",
            ticket_id=ticket_id,
            ticket_status=TicketStatus.rejected.value,
            detail="rejected",
        ).model_dump(),
    )
