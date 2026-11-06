from typing import Literal

from pydantic import BaseModel, EmailStr, Field, HttpUrl


class WebhookPayload(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    ticket_id: str = Field(..., min_length=1, max_length=128)
    customer_email: EmailStr
    message: str = Field(..., min_length=1)
    reply_url: HttpUrl


class WebhookResponse(BaseModel):
    status: Literal["ok"] = "ok"
    detail: Literal["queued", "duplicate_ignored"]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
