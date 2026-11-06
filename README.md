# AI-First Helpdesk Assistant Backend

An asynchronous, decoupled backend service for customer support triage with database-backed deduplication, worker task pipelines, and human-in-the-loop review. The system ingests inbound Helpdesk webhooks within strict response SLAs, deduplicates events at the database layer, retrieves grounded product context from a seeded Knowledge Base, generates cited response drafts via an LLM (or a deterministic Zero-Key Mock fallback), and exposes a minimal Operator Web Dashboard for moderation and outbound dispatch.

### Live Deployment (Fly.io)
- **Operator Web Dashboard:** [https://test-momentum.fly.dev/](https://test-momentum.fly.dev/)
- **Health Check:** [https://test-momentum.fly.dev/health](https://test-momentum.fly.dev/health)
- **Interactive Swagger UI:** [https://test-momentum.fly.dev/docs](https://test-momentum.fly.dev/docs)

---

## 1. Project Overview & Architecture

The service decouples latency-sensitive webhook ingestion from compute- and I/O-intensive draft generation using a four-container topology orchestrated via Docker Compose:

- **`api` (FastAPI + Uvicorn + Async SQLAlchemy 2.0):**
  - Exposes `POST /webhooks/helpdesk` for non-blocking webhook ingestion and database-backed event deduplication.
  - Serves the Jinja2 Operator Web Dashboard (`GET /`) and moderation endpoints (`POST /tickets/{id}/approve`, `POST /tickets/{id}/reject`).
  - Mounts a built-in Mock Helpdesk router (`/mock-helpdesk/*`) for end-to-end local verification without third-party dependencies.
  - Automatically executes Alembic schema migrations (`uv run alembic upgrade head`) and idempotent Knowledge Base seeding (`uv run python -m app.seed`) on container startup.
- **`db` (PostgreSQL 16):**
  - Acts as the system of record for `tickets`, `webhook_events`, and `knowledge_articles`.
  - Enforces inbound idempotency via unique constraints (`ON CONFLICT DO NOTHING RETURNING ...`) and coordinates outbound state transitions via conditional row-level updates.
- **`redis` (Redis 7):**
  - Serves as the Celery message broker, result backend, and global 60 RPM per-minute counter store for LLM invocations.
- **`worker` (Celery Worker):**
  - Executes background draft generation (`generate_ticket_draft`) across three decoupled phases (short-lived DB read, connection-free LLM/mock I/O, and guarded DB write) so database connections are not held open during slow provider calls.

```text
+-------------------+      POST /webhooks/helpdesk       +-------------------+
|  Helpdesk Client  | ---------------------------------> |   FastAPI (api)   |
|  (or Built-in     | <--------------------------------- |                   |
|   Mock Simulator) |       200 OK (queued / dup)        +---------+---------+
+-------------------+                                              |
          ^                                                        |
          | 4. POST {reply_url}                         1. Atomic Upsert &
          |    (3x retry + jitter)                         Enqueue Task
          |                                                        v
+---------+---------+                                    +-------------------+
|  Operator Web UI  |                                    |  PostgreSQL (db)  |
|  Approve / Reject | <--------------------------------> |  & Redis Broker   |
+-------------------+      3. Review & Atomic Lock       +---------+---------+
                                                                   |
                                                        2. Async RAG + LLM
                                                           (60 RPM limit)
                                                                   v
                                                         +-------------------+
                                                         |  Celery (worker)  |
                                                         +-------------------+
```

---

## 2. Quickstart & Local Setup

### Prerequisites
- Docker and Docker Compose (v2+)
- *(Optional for local non-Docker development)* Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)

### Start the Full Stack
No configuration or external API keys are required to run the service. Start all containers with a single command (first-time build duration depends on local network bandwidth and base image pulls):

```bash
docker compose up --build
```

On startup, the `api` entrypoint automatically applies Alembic migrations and seeds the PostgreSQL database with 5 CloudMetrics product articles before starting the HTTP server.

### Access Points
| Endpoint | URL | Description |
| :--- | :--- | :--- |
| **Operator Web UI** | [`http://localhost:8000/`](http://localhost:8000/) | Queue view, in-place draft editor, Approve/Reject controls, and one-click inbound ticket simulator |
| **Health Check** | [`http://localhost:8000/health`](http://localhost:8000/health) | Liveness probe returning `{"status": "ok"}` |
| **Interactive API Docs** | [`http://localhost:8000/docs`](http://localhost:8000/docs) | Swagger UI with pre-populated example payloads targeting the local Mock Helpdesk |

### Zero-Key Mock Mode vs. Live OpenAI Mode
- **Zero-Key Mock Mode (Default):** When `OPENAI_API_KEY` (and `LLM_API_KEY`) is unset or set to `"mock"`, the worker executes an intentional non-blocking delay (`await asyncio.sleep(random.uniform(5.0, 30.0))`) to simulate upstream LLM provider latency and produces deterministic, grounded draft responses citing the matched Knowledge Base article titles and URLs in the language of the inquiry (English or Ukrainian).
- **Live OpenAI Mode (Optional):** Create a `.env` file from `.env.example` and supply a valid key:
  ```env
  OPENAI_API_KEY=sk-...
  OPENAI_MODEL=gpt-4o-mini
  ```
  Then recreate the services:
  ```bash
  docker compose up -d --build api worker
  ```

---

## 3. Ticket State Machine

Every ticket progresses through a guarded state machine stored in `tickets.status`:

```text
[Webhook Arrives]
       │
       ▼
 pending_draft ──(Uncaught worker error)──► failed
       │
 (RAG + LLM succeeds & status is still pending_draft)
       │
       ▼
  draft_ready ───(Operator clicks Reject)─► rejected
       │
 (Operator clicks Approve: atomic edit + state lock)
       │
       ▼
    sending ─────(3 retries exhausted)────► failed
       │
 (Outbound POST returns 201 Created)
       │
       ▼
      sent
```

- **State Guards:**
  - `reject_ticket` only transitions tickets currently in `pending_draft` or `draft_ready`, preventing operators from overwriting tickets already in `sending` or `sent`.
  - `_process_ticket_draft` guards its completion write with `WHERE status = 'pending_draft'`, ensuring a ticket rejected while the worker was generating a draft is not overwritten back to `draft_ready`.

---

## 4. Engineering Trade-offs & Deep Architectural Decisions

### Trade-off 1: Direct Celery Enqueue vs. Transactional Outbox
- **Context & Decision:** Direct Celery enqueueing (`generate_ticket_draft.delay(...)`) immediately following the PostgreSQL transaction commit in `POST /webhooks/helpdesk`.
- **Trade-off & Rationale:** Prioritizes development velocity and low latency for the inbound webhook. However, this introduces a dual-write failure window between PostgreSQL and Redis. Because inbound deduplication commits `event_id` to `webhook_events` before publishing to the broker, **a dropped Redis publish is not self-healing**: if the process crashes or loses Redis connectivity after the database commit, subsequent client retries carrying the same `event_id` will receive `duplicate_ignored`, leaving the ticket stuck in `pending_draft` without a scheduled worker task.
- **When would I reconsider this decision?** When the system requires recovery from broker publish failures without manual intervention. Full self-healing requires either a Transactional Outbox pattern (persisting an outbox row in the same database transaction and publishing via CDC/Debezium or a polling relay) or a periodic background reconciliation sweeper (e.g., Celery Beat re-enqueueing tickets that remain in `pending_draft` beyond a staleness threshold).

---

### Trade-off 2: Celery Prefork vs. Alternative Worker Pools
- **Context & Decision:** Celery worker executed with a prefork pool (`--concurrency=2`, `--max-tasks-per-child=100`) combined with a persistent thread-local `asyncio` event loop runner (`run_async`) and Celery process lifecycle signals (`worker_process_init`, `worker_process_shutdown`).
- **Trade-off & Rationale:** Multi-process execution introduces baseline memory overhead per worker process compared to lightweight green-thread or unified event-loop alternatives (e.g., Gevent or native AsyncIO worker daemons). However, process isolation provides predictable runtime boundaries for async drivers (`asyncpg`), avoids cross-task event loop contamination, and contains unexpected native extension crashes. `--max-tasks-per-child=100` periodically recycles child processes to bound memory growth over time.
- **When would I reconsider this decision?** When vertical worker scaling and high concurrency become memory-constrained under heavy I/O workloads, justifying a transition to native async task frameworks (such as SAQ or ARQ) operating within a unified event loop.

---

### Trade-off 3: Operation-Scoped HTTP Clients vs. Lifecycle-Managed Connection Pools
- **Context & Decision:** Instantiating `httpx.AsyncClient` per request attempt inside `_send_webhook_request` rather than maintaining a long-lived global client pool.
- **Trade-off & Rationale:** Sacrifices TCP/TLS connection reuse across sequential outbound calls. In return, scoping the client to individual request attempts avoids cross-event-loop socket sharing between Uvicorn, Celery thread-local loops, and test runners, while ensuring fresh DNS/TCP/TLS negotiation on retries following `5xx` responses or socket timeouts. Furthermore, outbound dispatch targets dynamic `reply_url` destinations with operator review gaps where idle keep-alive sockets would frequently go stale.
- **When would I reconsider this decision?** When outbound throughput scales significantly against a small set of fixed, high-frequency upstream endpoints, making connection pool amortization critical. In that case, introduce explicit lifecycle-managed pools bound cleanly to specific process and event-loop runtimes.

---

### Trade-off 4: Weighted Lexical Retrieval vs. Vector Embeddings
- **Context & Decision:** In-database SQL keyword scoring (`ILIKE`, `CASE` weighting) with multilingual normalization, keyword count capping (`MAX_KEYWORDS = 20`), and Ukrainian-to-English stem synonym expansion instead of deploying `pgvector` or an external vector embedding service.
- **Trade-off & Rationale:** Forfeits semantic capture of abstract, out-of-vocabulary user queries. In return, it leverages existing PostgreSQL infrastructure without introducing cold-start latency, memory-heavy local embedding models, or external vector API dependencies. It delivers low-latency retrieval for a bounded, static knowledge base while remaining operational in Zero-Key Mock Mode.
- **When would I reconsider this decision?** When the knowledge base expands beyond static operational FAQs into hundreds or thousands of dynamic documents, or customer phrasing becomes sufficiently ambiguous to require dense semantic retrieval (hybrid lexical + vector search).

---

### Trade-off 5: Atomic State Transitions (CAS) vs. Locking Alternatives (`SELECT ... FOR UPDATE`)
- **Context & Decision:** Row-level compare-and-swap (CAS) state transitions in PostgreSQL (`UPDATE tickets SET draft_reply = :draft, status = 'sending' WHERE id = :id AND status = 'draft_ready' RETURNING ...`) rather than pessimistic transactions (`SELECT ... FOR UPDATE [SKIP LOCKED]`) or external distributed locks (e.g., Redis Redlock).
- **Trade-off & Rationale:** Comparing atomic CAS against `SELECT ... FOR UPDATE`, a pessimistic lock would require holding an open database transaction across external network I/O (or adding an extra round-trip before committing a state change). Atomic CAS transitions the state and commits immediately, releasing the database row lock and connection before any outbound HTTP call begins. This provides **at-most-once approval locking with at-least-once network delivery**: only a single concurrent approval request can transition the row from `draft_ready` to `sending`, while the outbound HTTP client retries up to 3 times on `5xx` or network errors. Because retries can deliver a duplicate payload if a previous `201` response was lost in flight, end-to-end deduplication at the receiving Helpdesk still requires an outbound `Idempotency-Key` header (such as `ticket_id`).
- **When would I reconsider this decision?** When a business workflow must coordinate state across multiple partitioned databases or external resources that cannot be gated by a single relational row transition.

---

## 5. Testing & Verification

### Automated Test Suite (`pytest`)
Run all tests locally using `uv`:
```bash
uv run pytest -v
```

**Test Coverage Includes:**
- **`tests/test_health.py`:** Liveness endpoint verification (`GET /health`).
- **`tests/test_webhooks.py`:** Inbound webhook ingestion, `event_id` deduplication (`queued` vs. `duplicate_ignored`), multiple distinct events for a single ticket, and `422` schema validation.
- **`tests/test_llm_rag.py`:** Weighted Knowledge Base retrieval across English queries, Ukrainian/Cyrillic queries with stem synonym expansion, stopword filtering, and zero-match fallbacks.
- **`tests/test_llm_service.py`:** Zero-Key Mock Mode latency simulation (`5.0–30.0s`), language-aware draft formatting with citations, Live OpenAI API mode (`AsyncOpenAI`), task state transitions (`pending_draft` -> `draft_ready` / `failed`), and race protection against overwriting concurrently rejected tickets.
- **`tests/test_moderation_and_dispatch.py`:** Operator dashboard rendering (`GET /`), atomic edit-and-approve flow, concurrent approval deduplication (`asyncio.gather` verifying a single outbound HTTP call), 3-attempt exponential backoff with jitter on `5xx` errors, guarded rejection transitions, and built-in Mock Helpdesk endpoints.

> **Testing Scope & Dialect Note:** The automated `pytest` suite runs against an in-memory SQLite database (`sqlite+aiosqlite:///:memory:`) with foreign-key enforcement enabled. While this validates application routing, state transition predicates, RAG scoring logic, and retry behavior, SQLite serializes write transactions at the database level. Row-level concurrency semantics under contention rely on PostgreSQL's `READ COMMITTED` row locking; validating multi-worker lock contention end-to-end requires integration testing against a live PostgreSQL instance.

### Manual End-to-End Verification
1. Open [`http://localhost:8000/`](http://localhost:8000/) in a browser and click **+ Simulate Inbound Ticket**.
2. A new ticket appears with status `pending_draft`. After the simulated LLM delay (5–30s in Mock Mode, or upon completion in Live OpenAI Mode), refresh the page to observe the status transition to `draft_ready` with a cited draft reply.
3. Optionally edit the draft text inside the textarea and click **Approve & Send** to trigger atomic outbound dispatch to `/mock-helpdesk/tickets/{ticket_id}/replies` and transition the ticket to `sent`.

---

## 6. Known Limitations & Production Hardening

Before deploying this service to an untrusted production environment, the following operational and security gaps should be addressed:

1. **Outbound SSRF Protection:**
   - Inbound webhook payloads supply the `reply_url` target. Although `follow_redirects=False` is enforced on the outbound `httpx.AsyncClient`, the service does not currently validate `reply_url` against an allowlist of trusted domains or block private/link-local IP ranges (e.g., `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`), leaving outbound dispatch exposed to Server-Side Request Forgery (SSRF).
2. **Webhook Signature Verification & Dashboard Authentication:**
   - `POST /webhooks/helpdesk` does not currently verify an HMAC-SHA256 request signature header from the upstream Helpdesk provider.
   - The Operator Web Dashboard (`GET /`) and moderation endpoints (`POST /tickets/{id}/approve`, `POST /tickets/{id}/reject`) lack authentication, CSRF tokens, and role-based access control (RBAC).
3. **In-Flight Crash Recovery (Orphaned `sending` State):**
   - If the API or worker process crashes after committing `status = 'sending'` and before completing the outbound HTTP lifecycle, the ticket remains stuck in `sending`. Production deployment requires a periodic reconciliation job that resets or re-dispatches tickets whose `sending` state exceeds a staleness threshold (`updated_at < NOW() - INTERVAL '2 minutes'`).
4. **Manual Recovery for `failed` Tickets:**
   - Tickets that transition to `failed` (due to exhausted outbound retries or unhandled worker errors) are displayed as read-only in the Operator UI. Adding a "Retry / Re-open" operator action is needed to recover failed tickets without database intervention.
5. **Broker Availability on Ingestion:**
   - If Redis is unreachable after the PostgreSQL commit in `POST /webhooks/helpdesk`, `generate_ticket_draft.delay(...)` raises an error while the `event_id` remains recorded in `webhook_events`. Implementing a Transactional Outbox or a stale `pending_draft` sweeper is required to guarantee eventual processing across broker outages.
