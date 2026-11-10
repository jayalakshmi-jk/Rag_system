# RAG Document Chat

Upload PDF, TXT or Markdown files and chat with them in **English, தமிழ் or Tanglish**.
Answers come **only from your own documents**, with page citations you can click to see the
exact highlighted passage. Every user's data is private to their account.

- Email + password and **Google** sign-in (Supabase Auth), sessions persist across visits
- Hybrid retrieval (pgvector + Postgres full-text, reciprocal rank fusion) over your ready documents
- Streaming answers (SSE), conversation history with search, feedback, daily question quota
- No OCR: scanned (image-only) PDFs are rejected with a clear message

## Stack

| Layer | Choice |
|---|---|
| Frontend | HTML, CSS, vanilla JS ES modules (no framework, no build step); `supabase-js` used for **auth only** (vendored, SRI-pinned) |
| Backend | Python 3.12, FastAPI, Pydantic Settings |
| Data | Supabase Postgres + pgvector (HNSW, cosine) + Auth + Row Level Security |
| Embeddings | Voyage AI `voyage-4`, 1024 dimensions, behind an `Embedder` interface |
| LLM | Anthropic Claude (`CLAUDE_MODEL` for answers, `CLAUDE_FAST_MODEL` for rewrites/titles/suggestions) |
| PDF | `pypdf` |
| Quality | pytest, Playwright (browser E2E), ruff, mypy (strict), Docker, GitHub Actions |

## Architecture

```
Browser ── supabase-js (sign-in only) ──► Supabase Auth
   │
   │  Bearer access token
   ▼
FastAPI ── verifies JWT locally (signature, exp, aud, iss; HS256 secret or JWKS)
   │        derives the user id ONLY from the verified `sub`
   ├─► Supabase Postgres (service key, every query filtered by user_id)
   │      documents · document_pages · chunks(vector 1024) · conversations · messages ·
   │      message_feedback · usage_daily      (RLS also enabled: defence in depth)
   ├─► Voyage  (embeddings)
   └─► Anthropic (answers, streaming)
```

Key decisions:

- **Server-side authorization.** The API uses the service key but never trusts a client-supplied
  user id; every query is scoped by the verified user id. RLS is on as a second layer
  (authenticated users get read-only access to their own rows; writes happen server-side only).
- **Quota is atomic and fair.** `consume_question` runs in SQL. A question is refunded whenever the
  request fails before the model produced output (embedding outage, provider error), so provider
  failures never burn a user's allowance. Greetings/thanks skip retrieval, the model and quota.
- **Grounding.** The system prompt limits answers to numbered excerpts, requires `[n]` citations,
  defines a localized "not found" sentence, and treats document text as untrusted data
  (prompt-injection defence; delimiter tags inside documents/questions are defanged).
- **Ingestion** runs as a background task with visible stages
  (`parsing → chunking → embedding → indexing → ready`) and always ends in `ready` or `failed`
  (timeout, cleanup of partial data, startup recovery of stale rows).
- **No extra infrastructure.** No Redis/queue: rate limiting is in-process (see limitations).
- **Observability.** Structured JSON logs with request ids; chat completions log latency, token
  counts, language and status. Document text, questions, passwords, tokens and keys are never logged
  (covered by tests).

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in the values (see below)
uvicorn app.main:create_app --factory --reload --port 8000
# open http://localhost:8000
```

### Tests and checks

```bash
ruff check . && ruff format --check .
mypy
pytest                                    # unit + API tests (in-memory Supabase fake)
# Real Postgres + pgvector integration tests (migrations, RLS, hybrid search, quota):
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest tests/db
# Browser E2E (headless Chromium via Playwright; fake auth server and in-memory backend):
playwright install chromium && pytest tests/e2e
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy, the full suite against a pgvector Postgres
service, and a Docker build with a smoke test and a non-root check.

## Environment variables

Copy `.env.example`. Required in production:

| Variable | Notes |
|---|---|
| `APP_ENV` | `production` enables HSTS and requires https URLs |
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_ANON_KEY` | Public; served to the browser for sign-in |
| `SUPABASE_SERVICE_KEY` | **Server only.** Bypasses RLS. Never commit or expose |
| `SUPABASE_JWT_SECRET` | Only if the project still uses the legacy HS256 secret. Projects with asymmetric signing keys are verified through JWKS automatically |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_FAST_MODEL` | Answers and fast helper calls |
| `VOYAGE_API_KEY`, `EMBED_MODEL`, `EMBED_DIM` | Must match `vector(1024)` in the migrations |
| `ALLOWED_ORIGINS` | Comma-separated https origins of your deployment |
| `TRUSTED_PROXY_COUNT` | Number of reverse proxies in front of the app (real client IP for rate limits) |
| `DAILY_QUESTION_LIMIT`, `MAX_UPLOAD_MB`, `MAX_DOCUMENTS_PER_USER`, `MAX_DOCUMENT_PAGES`, `MAX_QUESTION_LENGTH`, `MAX_CONTEXT_TOKENS`, `MAX_OUTPUT_TOKENS` | Cost controls |

The app fails fast at startup if required settings are missing or invalid.

## Manual setup: Supabase

1. Create a project at <https://supabase.com>.
2. **Database**: SQL Editor → run the three files in `supabase/migrations/` **in order**
   (`schema`, `functions`, `rls`). They enable `pgvector`, create the tables, the HNSW index, the
   `hybrid_search` / quota functions (callable by `service_role` only) and RLS policies.
   (Or use the Supabase CLI: `supabase db push`.)
3. **Keys**: Project Settings → API → copy the project URL, `anon` key and `service_role` key.
   If the project uses the legacy JWT secret, also copy it (Settings → JWT).
4. **Auth → URL Configuration**: set *Site URL* to your deployed URL and add
   `https://YOUR_DOMAIN/app` and `https://YOUR_DOMAIN/login` (plus `http://localhost:8000/...` for
   local work) to *Redirect URLs*.
5. **Auth → Providers → Email**: keep it enabled. With "Confirm email" on, new users get a
   confirmation mail (the UI explains this). Configure custom SMTP before launch; the built-in
   mailer is rate-limited.

## Manual setup: Google sign-in

1. Google Cloud Console → *APIs & Services* → *OAuth consent screen*: configure the app
   (External), add your domain and support email.
2. *Credentials* → *Create credentials* → *OAuth client ID* → type **Web application**.
   - Authorized JavaScript origins: `https://YOUR_DOMAIN` (and `http://localhost:8000` for dev)
   - Authorized redirect URI: `https://<project-ref>.supabase.co/auth/v1/callback`
3. Supabase → Auth → Providers → **Google**: enable it and paste the Client ID and Client secret.
4. Make sure `https://YOUR_DOMAIN/app` is in the Supabase *Redirect URLs* (step 4 above).

## Deployment

The app is a single container (`Dockerfile`: multi-stage, non-root, healthcheck).

```bash
docker build -t rag-document-chat .
docker run --rm -p 8000:8000 --env-file .env rag-document-chat
```

Any container host works (Render, Railway, Fly.io, Cloud Run, a VM with Docker):

1. Provide the environment variables above as secrets (not in the image).
2. Expose port `$PORT` (default 8000); use `/healthz` (liveness) and `/readyz` (database) probes.
3. Terminate TLS at the platform and set `TRUSTED_PROXY_COUNT` (usually `1`).
4. Run **a single instance first**: the rate limiter and the in-flight ingestion tasks are
   in-process. Scale up only after moving those to shared infrastructure (see limitations).
5. Smoke test: register, upload a small PDF, wait for *Ready*, ask a question, click a citation,
   then delete the account.

## Security summary

JWT verification with algorithm allow-list (no `none`, no algorithm confusion, service-role and
anonymous tokens rejected), deleted-account check, per-IP/per-user/failed-auth/upload/chat/account
rate limits, strict CSP without inline scripts or styles, `nosniff`, frame denial, strict CORS
allowlist, no API docs in production, magic-byte upload validation, filename sanitising,
output rendered via `textContent` only (no `innerHTML`), secrets redacted from logs, and
cross-user isolation tests for documents, chat, retrieval, conversations and feedback.

## Known limitations

- **No OCR**: image-only PDFs are rejected. Only PDF, TXT and MD (no DOCX/PPTX/XLSX).
- Plain-text and Markdown files have no real pages; they are split into virtual pages so
  citations still point to a location.
- **Single-instance design**: rate limits and background ingestion live in process memory. A
  restart marks interrupted documents as failed (the user re-uploads). Horizontal scaling needs a
  shared limiter and a job queue.
- The original file is not stored, only extracted text, so there is no "download original".
- Daily quota resets at midnight UTC.
- No document sharing, paid plans or admin dashboard (out of scope for V1).
- Regenerating an answer adds a new question/answer pair to the conversation history.
- Retrieval has a rerank hook (the `Reranker` interface in `app/services/retriever.py`) but ships without a
  cross-encoder reranker.
