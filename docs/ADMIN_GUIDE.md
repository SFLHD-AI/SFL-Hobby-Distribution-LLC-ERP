# SFL ERP System — Owner's Guide

A quick tour of daily operations. Log in at your SFL ERP System URL.
Demo logins after `seed_demo`: **admin / office / picker**, password `sflerp123`.

## Users & roles (sidebar → Users & roles, admin only)

Create accounts and assign each one a **role**. A role is a checklist of
modules its users may open (Orders, Inventory, Shipping, Deliveries, Reports…)
plus two switches: *can see costs/profits* and *full admin*. Four roles are
built in — Admin (everything), Office (day-to-day ops, no costs), Warehouse
(picking only), and **Fulfillment Manager** (deliveries + shipping) — and you
can create as many custom roles as you like. Users only see sidebar links for
modules their role allows; anything else bounces them back to their own pages.
Password resets are on the user's edit page.

## Daily flow

**1. Take an order** — Orders → New order. Pick customer (their ship-to and
per-SKU prices load automatically), add lines by scanning a barcode into the
scan box or picking from the product list. Check ☑ shipping / ☑ tax and set
amounts if applicable. **Confirm** to commit inventory.

**2. Pick it** — Confirm → *Send to pick queue*. Your warehouse person opens
`/pick/` on the iPad, taps the order, scans items (wrong item = red flash +
buzz; over-pick is blocked). Shorts get a reason and flow to backorders.
*Complete Pick* notifies the office; progress is visible live on the PO page.

**3. Finalize** — On the PO, *Finalize*. This is the moment stock is deducted
and the cost of each line is frozen (so later cost changes never rewrite
profit history). If quantities were short, one click creates the backorder PO.

**4. Invoice** — *Create invoice* from the PO, or Invoices → New to merge
several finalized POs from the same customer into one consolidated invoice.
Tax is 0% automatically with a valid resale certificate; otherwise your
default rate applies and the amount can be overridden on the invoice.
*PDF* to print, *Email* to send with the PDF attached.

**5. Ship** — PO → *Ship it*. ShipStation: push the order, tracking and label
cost sync back hourly (and email the customer). Pirate Ship: select orders on
the Shipping screen → export the batch CSV → import at pirateship.com → paste
tracking/cost back. uShip (pallets): push via API if configured, otherwise
enter the booked reference and cost manually.

**6. Get paid** — Invoices → Record payment. One check can cover several
invoices (*Auto-apply oldest first*). Partial payments are fine. Statements:
customer page → *Statement* (PDF) or *Email statement*.

## Local deliveries (your driver)

Check **🚐 Local delivery** on a PO and, once it's picked/finalized, it appears
on the **Deliveries** screen under "Ready to schedule". Pick a day, and your
driver (Fulfillment Manager role) sees that day's stops on his phone: tap a
stop for the address, phone, item list and a **Navigate** button; **🗺 Route
from warehouse** builds a Google Maps route from your address in Settings
through every open stop. At drop-off he takes a **proof photo** and taps *Mark
delivered* — the photo is saved on the shipment and the order flips to Shipped.

## Shipping weights & packages

Give each product a **unit weight** (product page, or the `weight_lbs` CSV
column). Every PO then shows its estimated ship weight. On a shipment, the
**package builder** splits the order into boxes: add a box with estimated
dimensions, assign items into it, and the box weight auto-sums from unit
weights (override it after weighing the packed box). Box dims + weights are
what you feed to Pirate Ship / ShipStation / uShip quotes.

## Inventory

- **Starting from an existing business?** Enter opening stock at *your* costs:
  on a new product's form fill "Opening quantity + your cost per unit"; on an
  existing product use an adjustment with the optional **unit cost** field
  (adding at a cost rolls the weighted average, removals never change it); or
  bulk-load via CSV import columns `opening_qty` and `opening_unit_cost`.
- **Receiving**: enter a supplier order (SKUs, qty, unit cost, freight/duties).
  *Mark received* puts stock in and rolls the **weighted average landed cost**.
- **Adjustments**: on any product page — reason code required, everything logged.
- **Cycle counts**: create a count sheet (all / category / bin), type counted
  quantities, apply — the system writes the deltas.
- **"Why is this SKU at this number?"** — every product page shows the full
  movement ledger: each receipt, sale, adjustment, count, and restock with
  before/after, user, time and source document.
- **Labels**: select products → print Code 128 labels for SKUs or bins.
- **CSV**: import/export products from the Inventory screen.

## Month-end (Reports)

- **Inventory valuation** — on-hand, unit cost, total value *as of any date*
  (computed from the ledger, not the current snapshot).
- **P&L** — revenue, COGS, gross profit, shipping costs, expenses, net profit.
- **Revenue/profit by customer**, **by day of week**, **sales by SKU/category/brand**.
- **AR aging** — current / 1-30 / 31-60 / 61-90 / 90+.
- Every report exports to CSV and PDF. **QuickBooks exports** (invoices,
  payments, expenses CSVs) are on the Reports page for your accountant.

## Certificates & tax

Upload resale certificates on the customer page. Expired/missing certs show
warnings on the dashboard and customer page. Per customer, choose what
happens without a valid cert: **charge tax** or **block finalization**.

## Settings (admin only)

Company name, PNG logo, address, phone, contacts, website — these appear on
invoices, POs, packing slips and statements. Also: default tax rate, invoice
layout (Modern/Classic), invoice footer, starting numbers and prefixes for
PO/Invoice/Credit memo, low-stock email digest.

## Safety nets

- Nothing is hard-deleted — deletions are recoverable soft-deletes.
- Global **Activity log** (admin) records who did what and when.
- Nightly `backup_db` keeps 14 compressed database backups.
- Global search (top bar) finds SKUs, customers, PO/invoice/tracking numbers.
