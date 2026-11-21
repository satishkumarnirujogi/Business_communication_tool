# Contributing

Thanks for helping. This project is used by small clinics, often without an IT department, so the bar is:
**safe by default, readable errors, no surprises.**

## Ground rules

- **No real patient data**, anywhere: not in issues, logs, screenshots, test fixtures or demo files. Use the
  demo clinic (`DEMO_MODE=true`) or made-up names such as "Lena Test" and `@example.com` addresses.
- **No secrets** in code, tests or commits. Settings go in `.env`, which is gitignored; document new ones in
  `.env.example`.
- **Safety rules stay in code, not in the prompt.** Emergency detection, the confirmation before booking, and
  rule checks must not depend on the model behaving. If you change them, add a test.
- **Errors are for receptionists.** Messages a clinic will see say what went wrong and what to do, in plain
  words. No stack traces or jargon.
- **Type hints everywhere.** `mypy` must pass.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -e "backend[dev]"        # Windows: .venv\Scripts\pip ...
cp .env.example .env                            # set LLM_PROVIDER / LLM_MODEL / LLM_API_KEY, DEMO_MODE=true
cd frontend && npm install && npm run build && cd ..
cd backend && ../.venv/bin/uvicorn app.main:app --reload
```

For frontend work, also run `npm run dev` in `frontend/`. It serves http://localhost:5173/ui/admin.html and
`/ui/chat.html`, and forwards `/api` to the backend on port 8000.

## Checks (CI runs the same on every push)

```bash
cd backend
ruff check .          # lint (ruff check . --fix fixes most findings)
mypy                  # type check
pytest -q             # tests; set TEST_DATABASE_URL to also run the Postgres/pgvector tests (that database is wiped)
cd ../frontend
npm run typecheck && npm run build
```

Tests never call a real LLM, WhatsApp, email server or embedding model: they use fakes. Keep it that way, so
the suite runs offline and costs nothing.

## Where things live

| Change | Look at |
|---|---|
| What the assistant may do | `backend/app/agent/tools.py` (tools), `config/rules.yaml` + `app/rules.py` (checks) |
| How it talks | `backend/app/agent/prompts.py`, `app/agent/language.py` |
| A new data source | `backend/app/data/base.py` (interface); add tests like `tests/test_sql_stores.py` |
| A new channel | `backend/app/channels/base.py` (interface), `channels/router.py` |
| Admin or chat UI | `frontend/src/admin/`, `frontend/src/chat/` |

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Keep each PR to one change, with tests for new behaviour and updated docs (README, `docs/`, `.env.example`).
3. Describe how you tested it. For UI changes, add a screenshot (demo data only).

## Reporting security problems

Please don't open a public issue. Use GitHub's **Report a vulnerability** (Security tab → Advisories) so it
can be fixed before details are public.

By contributing you agree that your contributions are licensed under the MIT License.
