import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update

from app.celery_app import celery_app
from app.database import AsyncSessionLocal, engine
from app.models import Ticket, TicketStatus

logger = logging.getLogger(__name__)


async def _update_ticket_draft(ticket_id: str, message: str) -> None:
    placeholder_draft = (
        f"Hello,\n\nThank you for contacting CloudMetrics Support regarding ticket "
        f"'{ticket_id}'. We have received your inquiry and prepared this initial draft "
        f"response.\n\nBest regards,\nCloudMetrics Support"
    )
    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(
                    draft_reply=placeholder_draft,
                    status=TicketStatus.draft_ready,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(stmt)
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.generate_ticket_draft")
def generate_ticket_draft(ticket_id: str, message: str) -> dict[str, str]:
    logger.info(
        "Picked up background task generate_ticket_draft for ticket_id=%s",
        ticket_id,
    )
    asyncio.run(_update_ticket_draft(ticket_id=ticket_id, message=message))
    logger.info(
        "Completed draft generation for ticket_id=%s (status=draft_ready)",
        ticket_id,
    )
    return {"ticket_id": ticket_id, "status": TicketStatus.draft_ready.value}

