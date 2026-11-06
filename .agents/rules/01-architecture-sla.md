# Rule: Architecture, Webhook SLA, and Queues

## Webhook SLA Constraint (< 2.0s)
- Helpdesk timeout is 2 seconds. Exceeding this triggers redelivery.
- The webhook endpoint `POST /webhooks/helpdesk` MUST NOT perform LLM calls, synchronous embedding generation, or complex external network requests.
- Flow for incoming request:
  1. Validate payload via Pydantic (`event_id`, `ticket_id`, `message`, `reply_url`).
  2. Perform atomic insert into PostgreSQL (`ON CONFLICT DO NOTHING`).
  3. Push task to Redis queue (ARQ or Celery).
  4. Return `200 OK` (`{"status": "ok", "detail": "queued"}`) in < 50ms.

## Outbound Helpdesk Dispatch
- Operator approval triggers a background task to POST to `reply_url`.
- Helpdesk returns `201 Created` on success.
- If Helpdesk returns 5xx or times out:
  - Retry up to 3 times with exponential backoff (e.g., using `tenacity`).
  - Do NOT re-trigger the action if status is already `sending` or `sent`.

## Health Check
- Provide `GET /health` returning status code 200:
  `{"status": "ok"}`