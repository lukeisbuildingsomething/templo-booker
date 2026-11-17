# Templo

Group scheduling, simplified. Templo is a date-poll app: create a poll, share a link, let everyone vote on dates, then confirm the winner and send calendar links. Live at [templobooker.com](https://templobooker.com).

## Features

- **Passwordless auth** — magic links sent by email, no passwords stored
- **Organizer approval** — visitors can sign in and vote; admins approve access to create polls
- **Email changes** — the current address stays active until the new address is confirmed
- **Date polls** — pick candidate dates on a calendar, invitees vote yes / no / maybe
- **Access modes** — public link (anyone with the link) or invite-only (email allowlist)
- **Poll close & confirm** — pick winning date(s), voters get Google/Outlook deeplinks and an `.ics` download
- **Tiers** — free (1 active poll, 15 dates); Pro (unlimited, custom URL slugs). Pro upgrades are manual via the admin panel — no payments yet
- **Admin panel** (`/admin`) — user management, tier upgrades, email settings
- **Marketing site** — home, pricing, how-it-works, who-it's-for, why-us, contact

## Tech stack

- **Backend:** Flask (single `main.py`), raw SQL via `psycopg2`, gunicorn
- **Database:** PostgreSQL — schema auto-created by `init_db()` at import time
- **Frontend:** Jinja2 templates + Tailwind via CDN (known tech debt, deliberate)
- **Email:** MailerSend HTTP API — API key and From address configured at runtime in Admin → Email (stored in the `app_settings` table, not env vars)

## Local development

Requires Python 3.11+ and a PostgreSQL database.

```bash
# Install dependencies (uv or pip)
uv sync            # or: pip install -e .

# Configure environment
cp .env.example .env
# Set SESSION_SECRET, DATABASE_URL; set SESSION_COOKIE_SECURE=0 for local http

# Run
python main.py     # dev server on PORT (default 8080)
```

### Environment variables

| Variable | Purpose |
|---|---|
| `SESSION_SECRET` | Flask session signing key (required) |
| `DATABASE_URL` | PostgreSQL connection string (required) |
| `PUBLIC_BASE_URL` | Canonical origin for email, share, and profile image URLs (default `https://templobooker.com`); use `http://127.0.0.1:8080` for local development |
| `ADMIN_EMAILS` | Comma-separated emails granted admin on first login |
| `PORT` | HTTP port (default 8080) |
| `FLASK_DEBUG` | `1` for debug mode |
| `SESSION_COOKIE_SECURE` | `1` (default) requires https; set `0` for local http |

## Smoke test

`smoke_test.py` exercises auth, poll create/vote/close, ICS output, tier limits, rate limiting, and the admin panel. `regression_test.py` covers host poisoning, organizer approval and migration, calendar updates, voting results, contact delivery failures, email changes, concurrent quota checks, and slug removal. Both expect a throwaway Postgres on port 55432; the regression suite resets that test database between cases:

```bash
docker run --rm -d --name templo-test-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=templo -p 55432:5432 postgres:16-alpine

python smoke_test.py   # exits 0 on success
python regression_test.py

docker stop templo-test-pg
```

## Deployment (Railway)

Deployed on [Railway](https://railway.app), project **Templo Booker**, two services:

- **DiamondDogsScheduler** — the Flask app, auto-deploys from `main`, serves templobooker.com (gunicorn behind Railway's TLS proxy; `ProxyFix` is configured for this)
- **Postgres** — the database, reachable in-network at `postgres.railway.internal`

Notes:

- The app crashes at boot if the database is unreachable (`init_db()` runs at import). If the app is down, check the Postgres service first.
- Useful CLI: `railway logs --service DiamondDogsScheduler`, `railway redeploy --service <name> --from-source -y`
- After first deploy, sign in with an `ADMIN_EMAILS` address and set the MailerSend API key in Admin → Email, or magic-link emails won't send.
- Set `PUBLIC_BASE_URL` to the deployment's public origin. Request Host and forwarded-host headers never determine emailed authentication URLs.
- The organizer approval migration preserves admins, legacy allowlisted users, approved account requests, and existing poll owners. Other existing voters can request organizer access; admins can approve them in User Management.
- Contact submissions are stored before email delivery and remain available in Admin → Contact messages if notifications fail.

## Local validation (no GitHub Actions)

See [validation and release status](docs/local-validation.md).
