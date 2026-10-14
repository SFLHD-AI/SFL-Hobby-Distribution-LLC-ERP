# SFL ERP System — Deployment Guide (Ubuntu VPS, nginx + gunicorn)

Two supported paths. **Path A (Docker) is recommended** — it is what
`docker-compose.yml` was built for and takes ~15 minutes.

---

## Path A — Docker Compose (recommended)

### 1. Server prep (Ubuntu 22.04/24.04)

```bash
sudo apt update && sudo apt upgrade -y
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
# firewall
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
```

### 2. Get the code and configure

```bash
sudo mkdir -p /srv/sfl-erp && sudo chown $USER /srv/sfl-erp
cd /srv/sfl-erp
# copy the source tree here (git clone, rsync, or unzip the delivered archive)
cp .env.example .env
nano .env    # set SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS, PUBLIC_BASE_URL, SMTP, Plaid/ShipStation keys
```

Required minimum in `.env`: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`,
`DJANGO_ALLOWED_HOSTS=yourdomain.com`, `DJANGO_CSRF_TRUSTED_ORIGINS=https://yourdomain.com`,
`POSTGRES_PASSWORD`. Set `PUBLIC_BASE_URL=https://yourdomain.com` so links in
the weekly review email and the Plaid Link redirect point at your server.
Plaid keys can go in `.env` or be pasted on the Settings screen later.

### 3. Launch

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
# optional demo data to explore first (includes a demo bank feed):
docker compose exec web python manage.py seed_demo
```

The app now answers on port 8000. On every start the web container runs
migrations, `setup_roles`, `setup_accounts` (chart of accounts + default
expense categories), `setup_schedules` (bank sync, recurring expenses, review
digest, ShipStation sync, low-stock digest) and collects static files. The
`worker` container runs the Django-Q cluster that executes those schedules —
if it is not running, bank feeds will not sync and digests will not go out.

### 4. nginx reverse proxy + HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo tee /etc/nginx/sites-available/sfl-erp <<'EOF'
server {
    listen 80;
    server_name erp.example.com;            # <-- your domain
    client_max_body_size 25m;               # cert/receipt/statement uploads

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/sfl-erp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d erp.example.com    # free HTTPS, auto-renews
```

HTTPS is required for Plaid Link in production (Plaid will not open the bank
login widget on a plain-HTTP page).

### 5. Nightly backups

```bash
crontab -e
# add:
0 2 * * * cd /srv/sfl-erp && docker compose exec -T web python manage.py backup_db >> /var/log/sfl-erp-backup.log 2>&1
```

Backups land in the `backups` docker volume (last 14 kept). To copy them
off-server, add an rsync/rclone line after the backup command — offsite
copies are strongly recommended now that the database is also your books.
The `media` volume holds receipt photos, imported statements and uploaded
certificates; back it up too.

### 6. Updating the app

```bash
cd /srv/sfl-erp
# update the code (git pull / rsync new version)
docker compose up -d --build     # rebuilds, migrates, restarts
```

---

## Path B — Bare metal (no Docker)

```bash
sudo apt install -y python3-venv python3-dev libpq-dev nginx postgresql redis \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 shared-mime-info

sudo -u postgres psql -c "CREATE USER sfl_erp WITH PASSWORD 'strongpass';"
sudo -u postgres psql -c "CREATE DATABASE sfl_erp OWNER sfl_erp;"

sudo mkdir -p /srv/sfl-erp && cd /srv/sfl-erp   # put source here
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

cp .env.example .env && nano .env   # add POSTGRES_HOST=127.0.0.1, REDIS_HOST=127.0.0.1
set -a; . ./.env; set +a
python manage.py migrate && python manage.py setup_roles && python manage.py setup_accounts \
    && python manage.py setup_schedules
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

systemd units:

```ini
# /etc/systemd/system/sfl-erp.service
[Unit]
Description=SFL ERP System gunicorn
After=network.target postgresql.service
[Service]
WorkingDirectory=/srv/sfl-erp
EnvironmentFile=/srv/sfl-erp/.env
Environment=POSTGRES_HOST=127.0.0.1 REDIS_HOST=127.0.0.1
ExecStart=/srv/sfl-erp/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3
Restart=always
[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/sfl-erp-worker.service
[Unit]
Description=SFL ERP System background worker (django-q)
After=network.target redis.service
[Service]
WorkingDirectory=/srv/sfl-erp
EnvironmentFile=/srv/sfl-erp/.env
Environment=POSTGRES_HOST=127.0.0.1 REDIS_HOST=127.0.0.1
ExecStart=/srv/sfl-erp/venv/bin/python manage.py qcluster
Restart=always
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now sfl-erp sfl-erp-worker
```

nginx + certbot: same as Path A step 4. Backups: same command, run from the
venv (`/srv/sfl-erp/venv/bin/python manage.py backup_db`).

---

## Environment variable reference

See `.env.example` — every variable is documented inline there, including
SMTP, `PUBLIC_BASE_URL`, Plaid (`PLAID_CLIENT_ID`, `PLAID_SECRET`,
`PLAID_ENV`), ShipStation, Shippo and uShip keys.

## Rebuilding the CSS (only if you edit templates)

The Tailwind stylesheet is pre-built at `static/css/app.css`. If you change
templates/classes, rebuild with the standalone CLI:

```bash
curl -sLo tailwindcss https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64
chmod +x tailwindcss
./tailwindcss -c tailwind.config.js -i static/css/input.css -o static/css/app.css --minify
```

## First-day checklist

1. Log in as your superuser → **Settings**: upload your PNG logo, company
   address/phone/website, contacts, default tax rate, invoice layout,
   starting numbers for PO / Invoice / Credit memo, alert email.
2. Settings → **Accounting** card: set the **books start date** (the first
   day this system is your book of record — usually the first day of the
   current year or month), fiscal year start month, which basis to show
   first (cash), your mileage rate and sales-tax filing frequency.
3. Confirm the chart of accounts exists: `setup_accounts` ran on start; open
   Accounting → Chart of accounts and rename or add expense categories to
   match how you think about spending (Expenses → Categories does the same).
4. Users & roles: create your office, warehouse and bookkeeper users and
   assign roles. Only roles that *can see costs* see cash, profit and equity.
5. **Banking → Accounts**: add every checking account and credit card. Enter
   the **opening balance as of the books start date** (for a card, the
   amount owed as a positive number) and that date as the opening date.
   Transactions before the opening date are skipped on import/sync.
6. **Connect banks**: either paste Plaid keys in Settings → Bank feed and use
   Banking → Connect a bank, or download a CSV from each bank covering the
   books start date to today and use Banking → Import CSV (see
   `docs/BANK_CONNECTIONS.md`). Check that each account's balance on
   Banking → Accounts matches your real statement; if not, adjust the opening
   balance.
7. Import products (Inventory → Import CSV) or add them by hand, with
   **opening quantity + your cost per unit** so inventory value is right from
   day one.
8. Add customers, upload resale certificates, set their price list / terms.
   Open invoices from before the books start date go in as *AR adjustment*
   invoices so receivables are right without inflating revenue.
9. Enter a supplier order in Receiving and "Mark received" to build stock
   and weighted average costs; fill in the supplier invoice number and due
   date so unpaid bills show up.
10. Work the Banking inbox once: categorize, match, create rules with
    "Always do this". Open Accounting → Balance sheet (accrual) and check it
    looks like your business. Then turn on the weekly review email in
    Settings → Bank feed.
11. When ShipStation keys are added to `.env`, restart (`docker compose up
    -d`) and tracking sync runs hourly automatically.
