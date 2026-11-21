## What and why

<!-- One change per PR. Link the issue: Fixes #… -->

## How I tested it

<!-- Commands you ran, what you clicked. UI changes: a screenshot with demo data. -->

## Checklist

- [ ] `ruff check .`, `mypy` and `pytest` pass in `backend/`; `npm run typecheck && npm run build` pass in `frontend/`
- [ ] Tests added or updated for new behaviour (especially anything touching bookings, rules or emergency handling)
- [ ] Docs updated (README, `docs/`, `.env.example` for new settings)
- [ ] No real patient data, secrets or API keys anywhere in the diff
