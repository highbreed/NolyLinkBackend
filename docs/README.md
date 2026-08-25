# OSCU Gateway

FastAPI integration gateway between client ERP/POS systems (SAP, other ERPs, POS) and KRA's
eTIMS OSCU API. See [`KRA_OSCU_FastAPI_Integration_Spec.md`](../KRA_OSCU_FastAPI_Integration_Spec.md)
in the repo root for the full technical specification this project implements.

## Status

Under active build, phase by phase — see the spec's "BUILD PHASES" section for the roadmap.
Currently: Phase 0 (project scaffold, `/v1/health`, Alembic baseline).

## Running locally

```bash
cp .env.example .env
make dev      # docker-compose up --build: app + postgres + redis (+ celery worker)
```

Then `curl localhost:8000/v1/health` should return `{"status": "ok"}`.

```bash
make test       # run pytest inside the app container
make migrate     # alembic upgrade head
make lint        # ruff
make fmt          # black
make typecheck    # mypy
```

## Pointing at KRA sandbox vs production

Set `KRA_ENV=sandbox` or `KRA_ENV=production` in `.env`. The gateway picks `KRA_BASE_URL_SBX` or
`KRA_BASE_URL_PROD` accordingly (see `app/core/config.py`). Always develop and test against
sandbox; production credentials and cutover are a deliberate, separate step (see the spec's
"Ask before assuming" section).

## Onboarding a new client / branch

Filled in as Phases 4 (device init) and 9 (client auth) land.

---

The rest of this document (client onboarding, branch/device onboarding walkthroughs) is
completed in Phase 11 once the endpoints it describes actually exist.
