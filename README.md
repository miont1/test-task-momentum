# AI-First Helpdesk Assistant Backend

An asynchronous, fault-tolerant backend service for customer support triage. The system ingests inbound Helpdesk webhooks within strict response SLAs, deduplicates events at the database layer, retrieves grounded product context from a seeded Knowledge Base, generates cited response drafts via an LLM (or a deterministic Zero-Key Mock fallback), and exposes a minimal Operator Web Dashboard for human-in-the-loop review and outbound dispatch.

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
  - Serves as the Celery message broker, result backend, and global 60 RPM sliding-window rate-limit store for LLM invocations.
- **`worker` (Celery Worker):**
  - Executes background draft generation (`generate_ticket_draft`) across three decoupled phases (short-lived DB read, connection-free LLM/mock I/O, and guarded DB write) so database connections are never held idle during slow provider calls.

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

## 2. Quickstart & Local Setup (< 2 Minutes)

### Prerequisites
- Docker and Docker Compose (v2+)
- *(Optional for local non-Docker development)* Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)

### Start the Full Stack
No configuration or external API keys are required to run the service. Start all containers with a single command:

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
- **Zero-Key Mock Mode (Default):** When `OPENAI_API_KEY` (and `LLM_API_KEY`) is unset or set to `"mock"`, the worker emulates realistic LLM network latency using a non-blocking `asyncio.sleep(random.uniform(5.0, 30.0))` and produces deterministic, grounded draft responses citing the matched Knowledge Base article titles and URLs in the language of the inquiry (English or Ukrainian).
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
  - `_process_ticket_draft` guards its completion write with `WHERE status = 'pending_draft'`, ensuring a ticket rejected while the worker was generating a draft is never overwritten back to `draft_ready`.

---

## 4. Engineering Trade-offs & Deep Architectural Decisions

### Trade-off 1: Direct Celery Enqueue vs. Transactional Outbox
- **Context & Decision:** Direct Celery enqueueing (`generate_ticket_draft.delay(...)`) immediately following the PostgreSQL transaction commit in `POST /webhooks/helpdesk`.
- **Trade-off & Rationale:** Prioritizes development velocity and low latency for the inbound webhook. However, this introduces a transient dual-write failure window: if the application process crashes or loses network connectivity to Redis between the database commit and the broker enqueue, the event is persisted without a corresponding scheduled task. Inbound deduplication ensures subsequent client retries are safe, but it does not proactively recover a dropped publish if `webhook_events` already committed the `event_id`.
- **When would I reconsider this decision?** When the system scales to zero-tolerance SLAs where dropped webhooks cannot rely on client-side retries. In that scenario, introduce a Transactional Outbox pattern backed by a dedicated CDC pipeline (e.g., Debezium) or a polling background publisher with idempotent consumer workers.

---

### Trade-off 2: Celery Prefork vs. Alternative Worker Pools
- **Context & Decision:** Celery worker executed with a prefork pool (`--concurrency=2`, `--max-tasks-per-child=100`) combined with a persistent thread-local `asyncio` event loop runner (`run_async`) and Celery process lifecycle signals (`worker_process_init`, `worker_process_shutdown`).
- **Trade-off & Rationale:** Multi-process execution introduces baseline memory overhead per worker process compared to lightweight green-thread or unified event-loop alternatives (e.g., Gevent or native AsyncIO worker daemons). However, process isolation provides predictable runtime boundaries for async drivers (`asyncpg`), prevents cross-task event loop contamination, and contains unexpected native extension crashes. `--max-tasks-per-child=100` periodically recycles processes to cap memory creep.
- **When would I reconsider this decision?** When vertical worker scaling and high concurrency become memory-constrained under heavy I/O workloads, justifying a transition to fully native async task frameworks (such as SAQ or ARQ) operating within a unified event loop.

---

### Trade-off 3: Operation-Scoped HTTP Clients vs. Lifecycle-Managed Connection Pools
- **Context & Decision:** Instantiating `httpx.AsyncClient` locally within dispatch routines (`_send_webhook_request`) rather than maintaining a long-lived global singleton pool.
- **Trade-off & Rationale:** Sacrifices TCP/TLS handshake reuse across sequential requests. However, it completely eliminates cross-event-loop socket binding errors between Uvicorn, Celery thread-local loops, and `pytest` runners. Furthermore, outbound dispatch targets dynamic, multi-tenant `reply_url` destinations with operator idle gaps lasting minutes or hours; a long-lived idle pool would accumulate stale keep-alive connections subject to remote resets. On network errors or 5xx responses, instantiating a fresh client on retry ensures a clean DNS/socket resolution.
- **When would I reconsider this decision?** When outbound throughput scales significantly against a small set of fixed, high-frequency upstream endpoints, making connection pool amortization critical. In that case, introduce explicit lifecycle-managed pools bound cleanly to specific process/loop runtimes.

---

### Trade-off 4: Weighted Lexical Retrieval vs. Vector Embeddings
- **Context & Decision:** In-database SQL keyword scoring (`ILIKE`, `CASE` weighting) with multilingual normalization and Ukrainian-to-English stem synonym expansion instead of deploying `pgvector` or an external vector embedding service.
- **Trade-off & Rationale:** Forfeits semantic capture of abstract, out-of-vocabulary user queries. In return, it leverages existing PostgreSQL infrastructure without introducing cold-start latency, memory-heavy local embedding models, or external vector API dependencies. It delivers low-latency retrieval for a bounded, static knowledge base while remaining 100% operational in Zero-Key Mock Mode.
- **When would I reconsider this decision?** When the knowledge base expands beyond static operational FAQs into hundreds/thousands of dynamic documents, or customer phrasing becomes sufficiently ambiguous to require dense semantic retrieval (hybrid lexical + vector search).

---

### Trade-off 5: Atomic Conditional State Transitions vs. Distributed Locks
- **Context & Decision:** Row-level atomic state machine transitions in PostgreSQL (`UPDATE tickets SET draft_reply = :draft, status = 'sending' WHERE id = :id AND status = 'draft_ready' RETURNING ...`) rather than external distributed locking (e.g., Redis Redlock).
- **Trade-off & Rationale:** Relies on relational row-level exclusive locks during the update statement. However, PostgreSQL acts as the single source of truth, removing external distributed coordination, clock-skew vulnerabilities, and orphaned-lock edge cases caused by worker crashes. Any concurrent attempt to approve the same ticket receives `0` rows back and halts before executing network I/O, achieving effectively-once dispatch behavior through idempotency.
- **When would I reconsider this decision?** When a single business transaction must coordinate resources across multiple partitioned databases or external services that cannot be bounded by a single relational transaction.

---

## 5. Testing & Verification

### Automated Test Suite (`pytest`)
The test suite uses an isolated in-memory SQLite engine (`sqlite+aiosqlite:///:memory:`) with foreign-key enforcement enabled and mocks external Redis/HTTP boundaries for deterministic execution.

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

### Manual End-to-End Verification
1. Open [`http://localhost:8000/`](http://localhost:8000/) in a browser and click **+ Simulate Inbound Ticket**.
2. A new ticket appears with status `pending_draft`. After the simulated LLM delay (5–30s in Mock Mode, or immediately in Live OpenAI Mode), refresh the page to observe the status transition to `draft_ready` with a cited draft reply.
3. Optionally edit the draft text inside the textarea and click **Approve & Send** to trigger atomic outbound dispatch to `/mock-helpdesk/tickets/{ticket_id}/replies` and transition the ticket to `sent`.
