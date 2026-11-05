# CLAUDE.md

## Project
Support-ticket service: receives helpdesk webhooks, drafts replies with an LLM
using a knowledge base, an operator approves/rejects in a web UI, approved
replies are sent back via the helpdesk API. Full task: docs/TASK.md.

## Stack
Python 3.12, Django 5, PostgreSQL 16, gunicorn, httpx.
No Celery/Redis: the queue lives in Postgres. Everything runs via `docker compose up`.

## Commands
- Run: `docker compose up --build`
- Tests: `docker compose run --rm web python manage.py test`
- Migrations: `python manage.py makemigrations` (commit the files)
- Seed KB: `python manage.py seed_kb` (idempotent)

## Architecture invariants
- `POST /webhooks/helpdesk` only writes to the DB and returns 200 fast (< 2 s).
  Never call the LLM or any external service there.
- Idempotency: `Ticket.event_id` is unique; duplicates return 200 "duplicate".
- Ticket lifecycle: received -> drafting -> draft_ready -> approved -> sending -> sent
  (or rejected / failed).
- Workers claim rows with `SELECT ... FOR UPDATE SKIP LOCKED` plus a lease
  (`locked_until`); rows of crashed workers are picked up after the lease expires.
- LLM: 5-30 s per call, 60 req/min global limit -> async worker with a semaphore
  and a token bucket. Without an API key: still send the real request, ignore the
  result, sleep 5-30 s, return a static draft through the same code path.
- Customer reply: the same `reply_key` (UUID) is sent as `Idempotency-Key` on every
  retry; approved -> sending is an atomic conditional UPDATE.
- KB search: Postgres FTS (`simple` config) with prefix-stem queries. No embeddings
  (the service must work without an API key).

## Conventions
- Small commits with prefixes (feat:, fix:, test:, docs:, chore:). Never squash or
  rewrite history.
- Every behaviour that guards an invariant above needs a test.
- Config comes from env vars with safe defaults; `docker compose up` must work
  without a .env file.
- Keep the UI basic: server-rendered Django templates, no JS framework.
- Add dependencies only with a reason; keep requirements.txt in sync.