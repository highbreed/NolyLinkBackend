# Deploying kra-vscu-backend

Bare-metal, matching how every other service on this box runs (email_service, rra_addon,
notification-service, pearl_enterprise's outlet_manager_backend): gunicorn+Uvicorn workers
under systemd, bound to a unix socket, fronted by nginx. Postgres and Redis are the box's
existing shared instances — not a per-app container (docker-compose.yml/Dockerfile in the repo
root are for local dev only; nothing here depends on them).

**Nothing in this document has been run.** No production checkout exists yet, no systemd units
are installed, nothing is listening on a socket. This is the full path to get there, written so
it can be followed (or handed to someone else) step by step. `install.sh` is the only part that
touches anything outside this repo, and only once you run it yourself with `sudo`.

This is a live KRA fiscal integration — read the "KRA environment" section before doing
anything with `KRA_ENV`.

## 1. One-time: create the production checkout

Separate git checkout from the dev copy, same pattern as every sibling service:

```bash
sudo mkdir -p /home/dev/web_files/kra_vscu/production
sudo chown dev:dev /home/dev/web_files/kra_vscu/production
cd /home/dev/web_files/kra_vscu/production
git clone /home/dev/web_files/kra_vscu/kra_vscu_backend kra_vscu_backend  # or a real remote, once one exists
cd kra_vscu_backend
git checkout main   # see "No remote / no main branch yet" below if this doesn't exist

python3.11 -m venv venv
source venv/bin/activate
pip install .          # no [dev] extras in production — pulls in gunicorn (added this phase)
```

### No remote / no main branch yet

As of this write-up, this repo has no git remote configured and no `main` branch — only
`master`, with everything committed locally. Before this step is real, either:

- push `master` to a remote (GitHub/GitLab/etc.) and deploy from a `main`/release branch there, or
- `git clone` directly from this local path (as above) and `git checkout master` instead of
  `main`, accepting that "deploy" then means pulling from this exact machine's working copy.

Either way, decide this before wiring up `install.sh`'s rollout step (step 7) — it assumes a
normal `git pull origin main`.

## 2. Database

Dedicated database and role, not the shared dev one (`kra_vscu`, role `root`) — this is fiscal
data, not dev experimentation data. Needs the Postgres superuser:

```sql
CREATE USER kra_vscu_prod WITH PASSWORD '<pick-a-strong-password>';
CREATE DATABASE kra_vscu_production OWNER kra_vscu_prod;
```

Then in production's `.env` (step 3): `DATABASE_URL=postgresql+asyncpg://kra_vscu_prod:<password>@localhost:5432/kra_vscu_production`.

Migrations run automatically on every API unit start/restart (`ExecStartPre=... alembic upgrade
head` in `systemd/kra-vscu-backend-api.service`), so there's nothing to run by hand here beyond
the first `alembic upgrade head` call `install.sh` makes before starting the units.

## 3. `.env`

Copy `.env.example` to `/home/dev/web_files/kra_vscu/production/kra_vscu_backend/.env` and fill
in real values — **do not copy the dev `.env`** (it points at dev's database/Redis DB and
dev-only secrets). Generate fresh secrets rather than reusing dev's:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CMC_KEY_ENCRYPTION_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"  # JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"  # ADMIN_API_KEY
```

`CMC_KEY_ENCRYPTION_KEY` is the Fernet key protecting every branch's `cmcKey` column
(`app/services/cmc_key_manager.py`) — losing it or mixing it up between environments makes
every already-registered OSCU branch's stored key unreadable. Keep it backed up somewhere other
than this one `.env` file.

Redis: use logical DBs distinct from dev's (dev currently uses `/14` and `/15` — see dev's own
`.env`, not committed) so a production Celery worker/beat never shares a queue with a dev one,
e.g.:

```
REDIS_URL=redis://:<password>@localhost:6379/16
CELERY_BROKER_URL=redis://:<password>@localhost:6379/16
CELERY_RESULT_BACKEND=redis://:<password>@localhost:6379/17
```

(Ask whoever manages this box's Redis instance for its password and which logical DB numbers
are actually free — don't assume 16/17 are unused.)

## 4. KRA environment — read this before touching `KRA_ENV`

**Per explicit instruction when this was written: production deploys keep `KRA_ENV=sandbox`.
Do not change it to `production` as part of this rollout.** Going live against KRA's real
production endpoints is a separate, deliberate decision — not a side effect of standing up
infrastructure. When that decision is made, here's everything that's environment-split and
needs a real value at that point (all already wired, none of this needs code changes):

- `KRA_BASE_URL_PROD`/`KRA_RECEIPT_VERIFICATION_BASE_URL_PROD` — already have real values in
  `.env.example`, confirmed working (see `app/services/kra_receipt.py`'s docstring).
- `KRA_BASE_URL_VSCU_PROD` — effectively dead config (VSCU has no KRA-hosted registration call
  at all — see `.env.example`'s own note); almost certainly stays blank regardless of env.
- `VSCU_DVC_SRL_NO_PROD` — KRA issues this once, domain-wide, per environment. Must be
  obtained from KRA before any production VSCU branch is initialized.
- Every branch's real `cmcKey` (OSCU) is obtained from KRA's real device-init call once
  `KRA_ENV=production` — sandbox `cmcKey`s are not valid there and vice versa. Don't reuse a
  sandbox-registered branch's cmcKey/device state against production.
- `ADMIN_API_KEY`/`JWT_SECRET_KEY` should already be production-only secrets from step 3,
  never the sandbox-era ones.
- `BILLING_ENFORCEMENT_ENABLED` — a separate decision again (gateway's own tenant billing, not
  a KRA setting at all) — see `.env.example`'s own note on why flipping it on blindly locks out
  every existing customer with no `Subscription` row.

## 5. systemd units + nginx site

Provided in `deploy/systemd/*.service` and `deploy/nginx/kra-vscu-backend.conf`:

- `kra-vscu-backend-api` — gunicorn + `UvicornWorker`, 4 workers, bound to
  `unix:/var/sockets/kra_vscu_backend_api.sock`. Runs `alembic upgrade head` before every start.
- `kra-vscu-backend-worker` — Celery worker (outbox dispatch + KRA/VSCU reference-data pollers).
- `kra-vscu-backend-beat` — Celery beat scheduler (see `app/workers/celery_app.py`'s
  `beat_schedule` for exactly what runs on what interval).

All three assume the production checkout lives at
`/home/dev/web_files/kra_vscu/production/kra_vscu_backend` — edit the unit files first if it's
somewhere else.

The nginx site has `kra-vscu.dg.com` as a placeholder `server_name` and plain HTTP (`listen
80`), matching how every other site on this box currently runs. **Before this carries real
traffic**: confirm the real domain, and confirm whether this box's plain-HTTP setup is because
TLS terminates at another proxy layer upstream, or whether this site needs its own certbot
certificate — a fiscal integration handling KRA PINs and receipt signatures over unencrypted
HTTP on the public-facing hop is not acceptable for go-live.

## 6. Install

```bash
cd /home/dev/web_files/kra_vscu/production/kra_vscu_backend/deploy
sudo ./install.sh
```

Idempotent — copies the unit files, `daemon-reload`s, runs migrations, enables + starts (or
restarts, if already running) all 3 units, installs and symlinks the nginx site, `nginx -t`s,
and reloads nginx. Refuses to run without `sudo`, and checks first that `.env`, the venv
(`gunicorn` present), exist.

Verify:

```bash
curl --unix-socket /var/sockets/kra_vscu_backend_api.sock http://localhost/v1/health
journalctl -u kra-vscu-backend-api -f
journalctl -u kra-vscu-backend-worker -f
journalctl -u kra-vscu-backend-beat -f
```

`/v1/health` (`app/api/v1/endpoints/health.py`) reports the last successful poll timestamp per
KRA resource — right after a fresh deploy, expect those to be empty/stale until the first beat
tick for each resource actually runs (`kra_poll_interval_*` in `.env.example`, up to 6 hours for
code lists).

## 7. Rolling out future updates

Manual, matching every other service here (no CI/CD on this box):

```bash
cd /home/dev/web_files/kra_vscu/production/kra_vscu_backend
git pull origin main   # or master — see "No remote / no main branch yet" above

source venv/bin/activate
pip install .          # if dependencies changed

sudo systemctl restart kra-vscu-backend-api kra-vscu-backend-worker kra-vscu-backend-beat
```

(The API unit's own `ExecStartPre` applies any new migration on that restart — no separate
migrate step needed.)

## 8. Rolling back

```bash
cd /home/dev/web_files/kra_vscu/production/kra_vscu_backend
git log --oneline -5                 # find the last-known-good commit
git checkout <commit-or-tag>
source venv/bin/activate && pip install .
# Only if the bad deploy shipped a migration that needs undoing — check `alembic history`
# first; most migrations here are additive and don't strictly need a downgrade to roll back:
venv/bin/alembic downgrade -1
sudo systemctl restart kra-vscu-backend-api kra-vscu-backend-worker kra-vscu-backend-beat
```

## Known gaps at the time of writing

- No remote git repository and no `main` branch — see step 1.
- `mypy app` currently reports 5 pre-existing errors (OSCU/VSCU `ItemSaveReq` schema overlap in
  `app/services/code_allowlist_service.py`/`code_cache_service.py`/`app/api/v1/endpoints/items.py`)
  — doesn't block deployment (the app runs and the full test suite passes), but worth a
  follow-up before this grows further.
- Domain name and TLS for the nginx site are both placeholders — see step 5.
