import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models import Ticket, TicketStatus

logger = logging.getLogger(__name__)


class DispatchRetryableError(Exception):
    """Raised when an outbound Helpdesk webhook call fails with a retryable error."""


async def _send_webhook_request(
    reply_url: str,
    payload: dict[str, str],
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5.0) as client:
        return await client.post(reply_url, json=payload)


async def dispatch_ticket_reply(
    ticket_id: str,
    *,
    is_retry: bool = False,
    max_attempts: int = 3,
    base_backoff: float = 0.5,
    raise_on_exhausted: bool = False,
) -> str:
    """Atomically lock a draft_ready ticket and dispatch its reply to reply_url.

    Returns one of:
    - 'sent': Reply delivered (HTTP 201 Created) and ticket marked 'sent'.
    - 'duplicate_or_not_ready': Ticket was not in 'draft_ready' state (aborted).
    - 'failed': All retry attempts failed and ticket was marked 'failed'.
    """
    async with AsyncSessionLocal() as session:
        if not is_retry:
            lock_stmt = (
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.status == TicketStatus.draft_ready,
                )
                .values(
                    status=TicketStatus.sending,
                    updated_at=datetime.now(timezone.utc),
                )
                .returning(Ticket.id, Ticket.reply_url, Ticket.draft_reply)
            )
            result = await session.execute(lock_stmt)
            row = result.one_or_none()
            if row is None:
                await session.rollback()
                logger.info(
                    "Aborting dispatch for ticket_id=%s: status is not draft_ready "
                    "(already sending, sent, or missing).",
                    ticket_id,
                )
                return "duplicate_or_not_ready"

            _, reply_url, draft_reply = row
            # Commit the 'sending' state immediately before external network I/O
            await session.commit()
        else:
            result = await session.execute(
                select(Ticket.id, Ticket.reply_url, Ticket.draft_reply).where(
                    Ticket.id == ticket_id,
                    Ticket.status == TicketStatus.sending,
                )
            )
            row = result.one_or_none()
            if row is None:
                logger.info(
                    "Aborting retry dispatch for ticket_id=%s: no longer in sending state.",
                    ticket_id,
                )
                return "duplicate_or_not_ready"
            _, reply_url, draft_reply = row

    payload = {
        "ticket_id": ticket_id,
        "message": draft_reply or "",
    }

    last_error: Exception | None = None
    delivered = False

    for attempt in range(1, max_attempts + 1):
        try:
            response = await _send_webhook_request(reply_url, payload)
            if response.status_code == 201:
                delivered = True
                break

            if response.status_code >= 500:
                last_error = DispatchRetryableError(
                    f"Helpdesk returned HTTP {response.status_code}"
                )
                logger.warning(
                    "Outbound dispatch attempt %d/%d for ticket_id=%s failed with HTTP %d",
                    attempt,
                    max_attempts,
                    ticket_id,
                    response.status_code,
                )
            else:
                # 4xx client error is non-retryable
                logger.error(
                    "Outbound dispatch for ticket_id=%s rejected with non-retryable HTTP %d",
                    ticket_id,
                    response.status_code,
                )
                break
        except httpx.HTTPError as exc:
            last_error = DispatchRetryableError(str(exc))
            logger.warning(
                "Outbound dispatch attempt %d/%d for ticket_id=%s raised HTTPError: %s",
                attempt,
                max_attempts,
                ticket_id,
                exc,
            )

        if attempt < max_attempts and base_backoff > 0:
            await asyncio.sleep(base_backoff * (2 ** (attempt - 1)))

    async with AsyncSessionLocal() as session:
        if delivered:
            sent_stmt = (
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(
                    status=TicketStatus.sent,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(sent_stmt)
            await session.commit()
            logger.info("Successfully dispatched reply for ticket_id=%s (status=sent)", ticket_id)
            return TicketStatus.sent.value

        if raise_on_exhausted and last_error is not None:
            raise last_error

        failed_stmt = (
            update(Ticket)
            .where(Ticket.id == ticket_id)
            .values(
                status=TicketStatus.failed,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.execute(failed_stmt)
        await session.commit()
        logger.error("Failed to dispatch reply for ticket_id=%s (status=failed)", ticket_id)
        return TicketStatus.failed.value
