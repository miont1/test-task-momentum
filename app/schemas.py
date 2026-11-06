from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl


class WebhookPayload(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    ticket_id: str = Field(..., min_length=1, max_length=128)
    customer_email: EmailStr
    message: str = Field(..., min_length=1)
    reply_url: HttpUrl

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_id": "evt_1001",
                "ticket_id": "tkt_1001",
                "customer_email": "user@example.com",
                "message": "Our webhook delivery keeps failing with 5xx timeouts. How do retries work?",
                "reply_url": "http://localhost:8000/mock-helpdesk/tickets/tkt_1001/replies",
            }
        }
    )


class WebhookResponse(BaseModel):
    status: Literal["ok"] = "ok"
    detail: Literal["queued", "duplicate_ignored"]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ApproveTicketRequest(BaseModel):
    draft_reply: str | None = None


class TicketActionResponse(BaseModel):
    status: Literal["ok", "ignored"] = "ok"
    ticket_id: str
    ticket_status: str
    detail: str | None = None


class MockHelpdeskReplyPayload(BaseModel):
    ticket_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1)


class MockHelpdeskReplyResponse(BaseModel):
    status: Literal["created"] = "created"
    ticket_id: str
    message: str
