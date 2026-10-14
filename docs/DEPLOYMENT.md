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
nano .env    # set SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS, SMTP, ShipStation keys
```

Required minimum in `.env`: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`,
`DJANGO_ALLOWED_HOSTS=yourdomain.com`, `DJANGO_CSRF_TRUSTED_ORIGINS=https://yourdomain.com`,
`POSTGRES_PASSWORD`.

### 3. Launch

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
# optional demo data to explore first:
docker compose exec web python manage.py seed_demo
```

The app now answers on port 8000. Migrations, role groups, background-job
schedules and static files are handled automatically on every start.

### 4. nginx reverse proxy + HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo tee /etc/nginx/sites-available/sfl-erp <<'EOF'
server {
    listen 80;
    server_name cards.example.com;          # <-- your domain
    client_max_body_size 25m;               # cert/image uploads

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
sudo certbot --nginx -d cards.example.com    # free HTTPS, auto-renews
```

### 5. Nightly backups

```bash
crontab -e
# add:
0 2 * * * cd /srv/sfl-erp && docker compose exec -T web python manage.py backup_db >> /var/log/sfl-erp-backup.log 2>&1
```

Backups land in the `backups` docker volume (last 14 kept). To copy them
off-server, add an rsync/rclone line after the backup command — offsite
copies are strongly recommended.

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
python manage.py migrate && python manage.py setup_roles && python manage.py setup_schedules
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
SMTP, ShipStation, and uShip keys.

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
2. Django admin (`/django-admin/` → Users): create your office and warehouse
   users; put them in the **Office** or **Warehouse** group. Add **CanSeeCosts**
   to any office user allowed to see costs/margins.
3. Import products (Inventory → Import CSV) or add them by hand.
4. Add customers, upload resale certificates, set their price list / terms.
5. Enter a supplier order in Receiving and "Mark received" to build stock
   and weighted average costs.
6. When ShipStation keys are added to `.env`, restart (`docker compose up -d`)
   and tracking sync runs hourly automatically.
