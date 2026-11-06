import logging
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import select, update

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models import Ticket, TicketStatus
from app.services.dispatch import DispatchRetryableError, dispatch_ticket_reply
from app.services.knowledge_base import search_knowledge_base
from app.services.llm import generate_draft_reply
from app.utils.async_runner import run_async

logger = logging.getLogger(__name__)


async def _process_ticket_draft(ticket_id: str, message: str) -> str:
    try:
        # Phase 1 (DB read): Open short-lived session, load ticket and KB articles, then close.
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )
            ticket = result.scalar_one_or_none()
            if ticket is not None and ticket.status != TicketStatus.pending_draft:
                logger.info(
                    "Skipping draft generation for ticket_id=%s: status is already %s",
                    ticket_id,
                    ticket.status.value,
                )
                return ticket.status.value

            effective_message = ticket.message if ticket is not None else message
            articles = await search_knowledge_base(
                session=session,
                query=effective_message,
                limit=2,
            )

        # Phase 2 (Slow I/O): Execute LLM / mock sleep without holding any DB connection.
        draft_text = await generate_draft_reply(
            customer_message=effective_message,
            articles=articles,
        )

        # Phase 3 (DB write): Open fresh short-lived session and guard status == pending_draft.
        async with AsyncSessionLocal() as session:
            stmt = (
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.status == TicketStatus.pending_draft,
                )
                .values(
                    draft_reply=draft_text,
                    status=TicketStatus.draft_ready,
                    updated_at=datetime.now(timezone.utc),
                )
                .returning(Ticket.status)
            )
            update_res = await session.execute(stmt)
            updated_status = update_res.scalar_one_or_none()
            await session.commit()

            if updated_status is None:
                logger.info(
                    "Ticket %s was modified concurrently (e.g. rejected); draft discard.",
                    ticket_id,
                )
                cur_res = await session.execute(
                    select(Ticket.status).where(Ticket.id == ticket_id)
                )
                cur_status = cur_res.scalar_one_or_none()
                return (
                    cur_status.value
                    if cur_status is not None
                    else TicketStatus.pending_draft.value
                )

            return TicketStatus.draft_ready.value
    except Exception:
        logger.exception(
            "Failed to generate draft for ticket_id=%s; marking status=failed",
            ticket_id,
        )
        async with AsyncSessionLocal() as session:
            fail_stmt = (
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.status == TicketStatus.pending_draft,
                )
                .values(
                    status=TicketStatus.failed,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(fail_stmt)
            await session.commit()
        return TicketStatus.failed.value


@celery_app.task(name="app.tasks.generate_ticket_draft", rate_limit="60/m")
def generate_ticket_draft(ticket_id: str, message: str) -> dict[str, str]:
    logger.info(
        "Picked up background task generate_ticket_draft for ticket_id=%s",
        ticket_id,
    )
    final_status = run_async(_process_ticket_draft(ticket_id=ticket_id, message=message))
    logger.info(
        "Finished draft task for ticket_id=%s (status=%s)",
        ticket_id,
        final_status,
    )
    return {"ticket_id": ticket_id, "status": final_status}


@celery_app.task(
    name="app.tasks.send_approved_reply",
    bind=True,
    max_retries=3,
    default_retry_delay=2,
)
def send_approved_reply(self: Task, ticket_id: str) -> dict[str, str]:
    is_worker_context = bool(getattr(self.request, "id", None))
    retries = int(getattr(self.request, "retries", 0) or 0)
    logger.info(
        "Picked up send_approved_reply for ticket_id=%s (retry=%d/%d)",
        ticket_id,
        retries,
        self.max_retries,
    )

    if is_worker_context:
        try:
            final_status = run_async(
                dispatch_ticket_reply(
                    ticket_id=ticket_id,
                    already_locked=True,
                    is_retry=(retries > 0),
                    max_attempts=1,
                    base_backoff=0.0,
                    raise_on_exhausted=(retries < self.max_retries),
                )
            )
        except DispatchRetryableError as exc:
            backoff_seconds = 2**retries
            logger.warning(
                "Scheduling Celery retry %d/%d for ticket_id=%s in %ds: %s",
                retries + 1,
                self.max_retries,
                ticket_id,
                backoff_seconds,
                exc,
            )
            raise self.retry(exc=exc, countdown=backoff_seconds)
    else:
        final_status = run_async(
            dispatch_ticket_reply(
                ticket_id=ticket_id,
                already_locked=False,
                is_retry=False,
                max_attempts=3,
                base_backoff=0.01,
                raise_on_exhausted=False,
            )
        )

    return {"ticket_id": ticket_id, "status": final_status}
