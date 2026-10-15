# Deploying SFL ERP System on Railway

Railway runs the app from the repository's `Dockerfile`. You need three
services in one Railway project: **web**, **worker** (background jobs) and
**Postgres**, plus **Redis** (recommended). Total setup is about 15 minutes.

## 1. Create the project

1. Sign in at https://railway.app and click **New Project → Deploy from GitHub repo**.
2. Pick `SFLHD-AI/SFL-Hobby-Distribution-LLC-ERP` and the branch you want to
   run (use `claude/epic-bohr-xizhhj` until it is merged to `main`).
   Railway detects the `Dockerfile` and `railway.json` automatically.
3. In the same project click **+ New → Database → PostgreSQL**, then
   **+ New → Database → Redis**.

## 2. Configure the web service

Open the service Railway created from the repo and set these **Variables**
(Settings → Variables). Use the *Add Reference* button for the two URLs:

| Variable | Value |
|---|---|
| `DATABASE_URL` | reference → Postgres → `DATABASE_URL` |
| `REDIS_URL` | reference → Redis → `REDIS_URL` |
| `DJANGO_SECRET_KEY` | a long random string (`python -c "import secrets;print(secrets.token_urlsafe(50))"`) |
| `DJANGO_DEBUG` | `0` |
| `DJANGO_ALLOWED_HOSTS` | your Railway domain, e.g. `sfl-erp.up.railway.app` (add a custom domain later, comma-separated) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://sfl-erp.up.railway.app` |
| `PUBLIC_BASE_URL` | `https://sfl-erp.up.railway.app` |
| `MEDIA_ROOT` | `/app/media` |
| `DJANGO_SUPERUSER_USERNAME` | the username you want to log in with, e.g. `admin` |
| `DJANGO_SUPERUSER_PASSWORD` | a strong password for that user |

Optional: SMTP (`EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
`EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`) and `PLAID_CLIENT_ID` /
`PLAID_SECRET` / `PLAID_ENV` (these can also be entered on the in-app
Settings page instead).

Then **Settings → Networking → Generate Domain** to get the public URL
(the app also trusts `RAILWAY_PUBLIC_DOMAIN` automatically).

**Uploads survive redeploys only with a volume:** Settings → Volumes →
**Add Volume**, mount path `/app/media`. This stores receipts, logos,
certificates and delivery photos.

The start command is already defined in `railway.json` (`sh start.sh`): it
runs migrations, creates roles, the chart of accounts and job schedules,
collects static files and starts gunicorn on Railway's `$PORT`.

## 3. Add the worker service

Background jobs (bank sync, recurring expenses, weekly digest, ShipStation
tracking) run in a second service from the same repo:

1. **+ New → GitHub Repo** → same repository and branch.
2. Settings → **Start Command**: `sh worker.sh`
3. Settings → Networking: no public domain needed.
4. Variables: add the same `DATABASE_URL`, `REDIS_URL`, `DJANGO_SECRET_KEY`,
   `DJANGO_DEBUG=0` and the email variables (shared variables work well:
   Project → Settings → Shared Variables).

## 4. First login

Open the web service's **Deploy Logs** until you see gunicorn listening. The
start script creates the admin user from `DJANGO_SUPERUSER_USERNAME` /
`DJANGO_SUPERUSER_PASSWORD` on the first boot (the log shows
`superuser admin: created`) and gives it the Admin role, so you can sign in
at `https://<your-domain>/accounts/login/` right away. Once logged in you may
remove the two variables; the user stays in the database.

Optional demo data to explore first:

```bash
# Railway → web service → ⋯ → "Run a command" (or `railway run` with the CLI)
python manage.py seed_demo
```

Log in, go to **Settings**: company details, fiscal year start, books start
date, and (optionally) Plaid keys. Then **Banking → Accounts** to add your
checking account and card with their opening balances, and import a CSV or
connect through Plaid.

## 5. Backups

Railway's Postgres plugin has point-in-time backups on paid plans. You can
also run `python manage.py backup_db --dir /app/media/backups` from the
"Run a command" dialog (the volume keeps the files) or on a cron service.

## Troubleshooting

- **CSRF verification failed**: `DJANGO_CSRF_TRUSTED_ORIGINS` must include
  `https://` and match the exact domain you are visiting.
- **Static files missing**: check the deploy log for `collectstatic`; the
  app serves them with WhiteNoise, no extra service needed.
- **Receipts disappear after a deploy**: the `/app/media` volume is missing.
- **Bank sync never runs**: the worker service is not running or lacks
  `REDIS_URL`/`DATABASE_URL`.
