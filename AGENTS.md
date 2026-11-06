# Agent Directives: AI-First Helpdesk Backend

You are building a production-ready, fault-tolerant AI backend service for customer support triage.

## Core Objective
Ingest support tickets via webhook within strict SLA (<2s), match knowledge base articles, generate response drafts using LLM/Mock, and present them in a minimal operator web UI for approval and dispatch.

## Critical Rules & Priorities
1. **Never block the webhook:** The webhook endpoint must strictly acknowledge receipt in <100ms. All heavy operations (RAG search, LLM calls) MUST execute in background workers via Redis queue.
2. **Strict Idempotency:** Duplicate webhook events must be ignored at the database level.
3. **No Duplicate Dispatch:** The Helpdesk client must never receive a duplicate reply. Use atomic state locking before sending.
4. **Resilient LLM Mocking:** If no API key is set, emulate real latency (5–30s) and produce grounded replies using local KB context.
5. **Clean Commit History:** Commit code incrementally step-by-step as outlined in the module plan.

Consult the modular rules in `.agents/rules/` for detailed implementation constraints.