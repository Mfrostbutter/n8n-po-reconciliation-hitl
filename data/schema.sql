-- PO Reconciliation template: system-of-record stand-in for SAP (purchase_orders, po_lines)
-- and Snowflake (reconciled_po_responses). Synthetic data only.
create schema if not exists om_poc;
set search_path = om_poc;

create table if not exists vendors (
  vendor_id    text primary key,
  name         text not null,
  email_domain text not null
);

create table if not exists purchase_orders (
  po_number    text primary key,
  vendor_id    text not null references vendors(vendor_id),
  currency     text not null,
  order_date   date not null,
  status       text not null default 'open',
  total_amount numeric(14,2) not null
);

create table if not exists po_lines (
  po_number     text not null references purchase_orders(po_number),
  line_no       int  not null,
  sku           text not null,
  description   text not null,
  quantity      numeric(12,3) not null,
  unit_price    numeric(14,4) not null,
  line_total    numeric(14,2) not null,
  promised_date date,
  primary key (po_number, line_no)
);

create table if not exists reconciliations (
  id                bigserial primary key,
  email_id          text,
  email_from        text,
  email_subject     text,
  po_number         text,
  vendor_name       text,
  document_type     text,
  extracted         jsonb not null,
  rule_results      jsonb not null,
  ai_confidence     numeric(5,3),
  score             numeric(5,3),
  decision          text not null,      -- auto | review
  status            text not null,      -- auto_approved | pending_review | approved | rejected | posted | failed
  reviewer_decision text,
  idoc_number       text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create table if not exists idoc_outbox (
  id                bigserial primary key,
  reconciliation_id bigint references reconciliations(id),
  idoc_number       text not null unique,
  idoc_type         text not null,
  xml               text not null,
  status            text not null default 'queued',
  created_at        timestamptz not null default now()
);

-- Mirrors the Snowflake landing table the production build would write to.
create table if not exists reconciled_po_responses (
  reconciliation_id bigint primary key references reconciliations(id),
  po_number         text not null,
  vendor_name       text,
  document_type     text,
  currency          text,
  total_amount      numeric(14,2),
  line_count        int,
  ai_confidence     numeric(5,3),
  score             numeric(5,3),
  decision          text,
  idoc_number       text,
  posted_at         timestamptz not null default now()
);

insert into vendors values
  ('V-1001', 'Northwind Industrial Supply', 'northwind-supply.example'),
  ('V-1002', 'Apex Valve & Fitting Co.',    'apexvalve.example'),
  ('V-1003', 'Meridian Electrical Ltd.',    'meridian-elec.example')
on conflict do nothing;

insert into purchase_orders values
  ('PO-45001', 'V-1001', 'USD', '2026-10-01', 'open', 18450.00),
  ('PO-45002', 'V-1002', 'USD', '2026-10-03', 'open', 9360.00),
  ('PO-45003', 'V-1003', 'USD', '2026-10-06', 'open', 27300.00),
  ('PO-45004', 'V-1001', 'EUR', '2026-10-08', 'open', 4200.00)
on conflict do nothing;

insert into po_lines values
  ('PO-45001', 1, 'NW-BRG-6205', 'Deep groove ball bearing 6205-2RS', 300, 12.50, 3750.00, '2026-10-20'),
  ('PO-45001', 2, 'NW-SEAL-40',  'Oil seal 40x62x8 NBR',               500, 4.20,  2100.00, '2026-10-20'),
  ('PO-45001', 3, 'NW-GRS-EP2',  'Lithium EP2 grease, 18 kg pail',     60,  210.00, 12600.00, '2026-10-25'),
  ('PO-45002', 1, 'AV-GV-2IN',   'Gate valve 2in 150# CS flanged',     12,  480.00, 5760.00, '2026-10-30'),
  ('PO-45002', 2, 'AV-CV-2IN',   'Check valve 2in 150# CS swing',      8,   450.00, 3600.00, '2026-10-30'),
  ('PO-45003', 1, 'ME-MCC-BKT',  'MCC bucket 30A FVNR size 1',         10,  1850.00, 18500.00, '2026-11-10'),
  ('PO-45003', 2, 'ME-CBL-4C6',  'Cable 4C 6 AWG armoured, per m',     400, 22.00, 8800.00, '2026-11-10'),
  ('PO-45004', 1, 'NW-FLT-HYD',  'Hydraulic filter element 10um',      140, 30.00, 4200.00, '2026-10-28')
on conflict do nothing;
