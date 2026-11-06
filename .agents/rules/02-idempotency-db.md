# Rule: Idempotency & Database State Machine

## Inbound Deduplication & Ingestion Order
- Multiple identical webhooks can arrive simultaneously or sequentially.
- Schema requirements:
  - Table `tickets` with column `id` (matching `ticket_id`) as PRIMARY KEY / UNIQUE.
  - Table `webhook_events` with column `event_id` as PRIMARY KEY, and `ticket_id` referencing `tickets.id`.

- Processing flow on incoming webhook:
  1. **Upsert Ticket First:**
     Execute an atomic insert into `tickets` with `ON CONFLICT (id) DO NOTHING` (or do update for mutable fields if needed). This ensures the parent record exists without race conditions.
  2. **Atomic Event Insertion:**
     Execute atomic insert into `webhook_events`:
     ```sql
     INSERT INTO webhook_events (event_id, ticket_id, received_at)
     VALUES (:event_id, :ticket_id, NOW())
     ON CONFLICT (event_id) DO NOTHING
     RETURNING event_id;
     ```
  3. **Duplicate Check:**
     - If the insert returns `None` / 0 rows (the `event_id` has already been recorded): acknowledge immediately with `200 OK` (`{"status": "ok", "detail": "duplicate_ignored"}`) and **do not** enqueue a background task.
     - If the insert returns the `event_id`: push the task to the Redis queue for draft generation and return `200 OK` (`{"status": "ok", "detail": "queued"}`).

## Single-Delivery Guarantee (No Double Replies)
- Allowed ticket statuses: `pending_draft`, `draft_ready`, `rejected`, `sending`, `sent`, `failed`.
- When the operator clicks "Approve", the state transition must be atomic to prevent parallel dispatches:
  ```sql
  UPDATE tickets 
  SET status = 'sending', updated_at = NOW() 
  WHERE id = :ticket_id AND status = 'draft_ready' 
  RETURNING id;
  - If 0 rows are returned, the ticket is either already being sent or has already been delivered. Abort dispatch immediately.