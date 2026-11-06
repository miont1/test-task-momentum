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
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )
            ticket = result.scalar_one_or_none()
            effective_message = ticket.message if ticket is not None else message

            articles = await search_knowledge_base(
                session=session,
                query=effective_message,
                limit=2,
            )
            draft_text = await generate_draft_reply(
                customer_message=effective_message,
                articles=articles,
            )

            stmt = (
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(
                    draft_reply=draft_text,
                    status=TicketStatus.draft_ready,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(stmt)
            await session.commit()
            return TicketStatus.draft_ready.value
        except Exception:
            logger.exception(
                "Failed to generate draft for ticket_id=%s; marking status=failed",
                ticket_id,
            )
            await session.rollback()
            fail_stmt = (
                update(Ticket)
                .where(Ticket.id == ticket_id)
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
                is_retry=False,
                max_attempts=3,
                base_backoff=0.01,
                raise_on_exhausted=False,
            )
        )

    return {"ticket_id": ticket_id, "status": final_status}
