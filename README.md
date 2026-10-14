# SFL ERP System

Production-ready operations app for a wholesale sports card & collectibles
distributor: inventory with a full movement ledger and weighted-average landed
cost, customers + resale certificates, customer POs with commit/finalize flow,
mobile barcode picking, consolidated invoicing with PDF + email, payments/AR,
profit & month-end reporting, and shipping integrations (ShipStation API,
uShip adapter, Pirate Ship batch CSV).

**Stack:** Django 5.2 LTS · DRF · PostgreSQL · Django-Q2 + Redis · Tailwind CSS · WeasyPrint

## Quick start (Docker)

```bash
cp .env.example .env        # edit secrets
docker compose up -d --build
docker compose exec web python manage.py seed_demo   # demo data
# open http://localhost:8000 — admin / sflerp123
```

## Quick start (local dev, SQLite)

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py setup_roles
python manage.py seed_demo
python manage.py runserver
```

## Tests

```bash
python manage.py test        # 36 tests over the money-critical paths
```

## Docs

- `docs/SCHEMA.md` — data model
- `docs/DEPLOYMENT.md` — Ubuntu VPS + nginx + gunicorn (Docker and bare-metal)
- `docs/ADMIN_GUIDE.md` — owner's user guide
- `.env.example` — every environment variable documented

## Layout

```
config/      settings, urls
core/        settings singleton, numbering, activity log, roles, PDF utils
inventory/   products, ledger, receiving/landed cost, cycle counts, price lists
customers/   customers, contacts, addresses, resale certificates
orders/      customer POs, status machine, picking API + mobile UI
invoicing/   invoices, credit memos, PDF layouts (modern/classic)
payments/    payments, applications, AR
expenses/    overhead expense tracking
shipping/    Shipment model + providers/ (ShippingProvider interface)
reports/     valuation, P&L, sales, aging, QuickBooks exports
templates/   Tailwind UI + WeasyPrint PDF templates
```
