"""Look-alike tables for the large-schema eval variants (chinook_xl, pagila_xl).

A real company database has hundreds of tables, many of them near-duplicates of the ones a
question needs: a CRM copy of `customer`, a finance `invoice`, archived payments, analytics marts.
These ~180 empty tables in 12 schemas put the eval databases in the 150-300 table range, where
the schema no longer fits the prompt and retrieval has to pick the right few tables among
decoys. The sample data stays untouched in `public`, so every gold query still works.

Deterministic: the same DDL every time, so retrieval scores stay comparable between runs.
Column spec: "name type", or "name -> schema.table" for an integer foreign key.
"""
import re

SCHEMAS: dict[str, dict[str, str]] = {
    "crm": {
        "account": "name text, industry text, region text, annual_revenue numeric(12,2), owner_id -> hr.employee",
        "contact": "account_id -> crm.account, first_name text, last_name text, email text, phone text, title text",
        "customer": "account_id -> crm.account, customer_code text, segment text, lifetime_value numeric(12,2), status text",
        "lead": "first_name text, last_name text, company text, source text, score int, status text",
        "opportunity": "account_id -> crm.account, name text, stage text, amount numeric(12,2), close_date date",
        "activity": "contact_id -> crm.contact, kind text, subject text, due_date date, completed boolean",
        "note": "account_id -> crm.account, body text, author_id -> hr.employee",
        "customer_address": "customer_id -> crm.customer, line1 text, city text, country text, postal_code text",
        "customer_tag": "customer_id -> crm.customer, tag text",
        "pipeline_stage": "name text, position int, probability numeric(5,2)",
        "territory": "name text, region text, manager_id -> hr.employee",
        "account_team": "account_id -> crm.account, employee_id -> hr.employee, role text",
        "email_message": "contact_id -> crm.contact, subject text, sent_at timestamptz, opened boolean",
        "call_log": "contact_id -> crm.contact, duration_seconds int, outcome text, called_at timestamptz",
        "customer_feedback": "customer_id -> crm.customer, rating int, comment text, submitted_at timestamptz",
    },
    "sales": {
        "order": "customer_id -> crm.customer, order_number text, status text, total numeric(12,2), ordered_at timestamptz",
        "order_item": "order_id -> sales.order, product_id -> product.item, quantity int, unit_price numeric(10,2)",
        "quote": "account_id -> crm.account, total numeric(12,2), valid_until date, status text",
        "quote_line": "quote_id -> sales.quote, product_id -> product.item, quantity int, unit_price numeric(10,2)",
        "store": "name text, city text, country text, opened_on date, manager_id -> hr.employee",
        "sales_rep": "employee_id -> hr.employee, quota numeric(12,2), territory_id -> crm.territory",
        "commission": "sales_rep_id -> sales.sales_rep, order_id -> sales.order, amount numeric(10,2)",
        "discount": "code text, percent numeric(5,2), starts_on date, ends_on date",
        "order_discount": "order_id -> sales.order, discount_id -> sales.discount",
        "return": "order_id -> sales.order, reason text, refunded_amount numeric(10,2), returned_at timestamptz",
        "shipment": "order_id -> sales.order, carrier text, tracking_number text, shipped_at timestamptz",
        "price_list": "name text, currency text, valid_from date",
        "price_list_item": "price_list_id -> sales.price_list, product_id -> product.item, price numeric(10,2)",
        "sales_target": "store_id -> sales.store, month date, target_amount numeric(12,2)",
        "store_visit": "store_id -> sales.store, visit_date date, visitors int",
    },
    "finance": {
        "invoice": "customer_id -> crm.customer, invoice_number text, issued_on date, due_on date, total numeric(12,2), status text",
        "invoice_line": "invoice_id -> finance.invoice, description text, quantity int, unit_price numeric(10,2)",
        "payment": "invoice_id -> finance.invoice, amount numeric(12,2), method text, paid_at timestamptz",
        "refund": "payment_id -> finance.payment, amount numeric(12,2), reason text, refunded_at timestamptz",
        "ledger_account": "code text, name text, kind text",
        "journal_entry": "entry_date date, memo text, posted boolean",
        "journal_line": "journal_entry_id -> finance.journal_entry, ledger_account_id -> finance.ledger_account, debit numeric(12,2), credit numeric(12,2)",
        "budget": "department_id -> hr.department, fiscal_year int, amount numeric(12,2)",
        "expense": "employee_id -> hr.employee, category text, amount numeric(10,2), spent_on date, approved boolean",
        "tax_rate": "country text, region text, rate numeric(5,2)",
        "currency_rate": "currency text, rate_to_usd numeric(12,6), rate_date date",
        "bank_account": "name text, iban text, currency text",
        "bank_transaction": "bank_account_id -> finance.bank_account, amount numeric(12,2), booked_on date, description text",
        "revenue_recognition": "invoice_id -> finance.invoice, recognized_on date, amount numeric(12,2)",
        "credit_note": "invoice_id -> finance.invoice, amount numeric(12,2), issued_on date",
    },
    "hr": {
        "employee": "first_name text, last_name text, email text, hired_on date, department_id -> hr.department, manager_id -> hr.employee, title text",
        "department": "name text, cost_center text",
        "job_title": "name text, level int",
        "salary": "employee_id -> hr.employee, amount numeric(12,2), effective_from date",
        "leave_request": "employee_id -> hr.employee, kind text, starts_on date, ends_on date, approved boolean",
        "timesheet": "employee_id -> hr.employee, work_date date, hours numeric(5,2), project text",
        "performance_review": "employee_id -> hr.employee, reviewer_id -> hr.employee, rating int, reviewed_on date",
        "training_course": "name text, provider text, hours int",
        "training_enrollment": "employee_id -> hr.employee, training_course_id -> hr.training_course, completed_on date",
        "benefit": "name text, provider text, monthly_cost numeric(10,2)",
        "employee_benefit": "employee_id -> hr.employee, benefit_id -> hr.benefit, enrolled_on date",
        "office": "name text, city text, country text, capacity int",
        "staff_member": "employee_id -> hr.employee, office_id -> hr.office, badge_number text, active boolean",
        "applicant": "first_name text, last_name text, email text, applied_for text, status text",
        "interview": "applicant_id -> hr.applicant, interviewer_id -> hr.employee, scheduled_at timestamptz, outcome text",
    },
    "product": {
        "item": "sku text, name text, category_id -> product.category, list_price numeric(10,2), active boolean",
        "category": "name text, parent_id -> product.category",
        "brand": "name text, country text",
        "item_brand": "item_id -> product.item, brand_id -> product.brand",
        "attribute": "name text, data_type text",
        "item_attribute": "item_id -> product.item, attribute_id -> product.attribute, value text",
        "review": "item_id -> product.item, customer_id -> crm.customer, rating int, body text, created_on date",
        "bundle": "name text, price numeric(10,2)",
        "bundle_item": "bundle_id -> product.bundle, item_id -> product.item, quantity int",
        "media_asset": "item_id -> product.item, url text, kind text",
        "release": "item_id -> product.item, version text, released_on date",
        "supplier": "name text, country text, contact_email text",
        "item_supplier": "item_id -> product.item, supplier_id -> product.supplier, cost numeric(10,2)",
        "rating_summary": "item_id -> product.item, average_rating numeric(3,2), review_count int",
        "genre_tag": "item_id -> product.item, genre text",
    },
    "inventory": {
        "warehouse": "name text, city text, country text",
        "stock_level": "warehouse_id -> inventory.warehouse, item_id -> product.item, quantity int, updated_on date",
        "inventory_item": "warehouse_id -> inventory.warehouse, item_id -> product.item, serial_number text, status text",
        "stock_movement": "item_id -> product.item, from_warehouse_id -> inventory.warehouse, to_warehouse_id -> inventory.warehouse, quantity int, moved_at timestamptz",
        "purchase_order": "supplier_id -> product.supplier, ordered_on date, status text, total numeric(12,2)",
        "purchase_order_line": "purchase_order_id -> inventory.purchase_order, item_id -> product.item, quantity int, unit_cost numeric(10,2)",
        "receipt": "purchase_order_id -> inventory.purchase_order, received_on date",
        "stock_count": "warehouse_id -> inventory.warehouse, counted_on date, counted_by_id -> hr.employee",
        "stock_count_line": "stock_count_id -> inventory.stock_count, item_id -> product.item, counted_quantity int",
        "reorder_rule": "item_id -> product.item, minimum_quantity int, reorder_quantity int",
        "bin_location": "warehouse_id -> inventory.warehouse, code text",
        "damaged_goods": "item_id -> product.item, quantity int, reported_on date, reason text",
        "store_location": "name text, address text, city text, country text, opened_on date",
        "rental_unit": "item_id -> product.item, store_location_id -> inventory.store_location, condition text",
        "asset_checkout": "rental_unit_id -> inventory.rental_unit, employee_id -> hr.employee, checked_out_at timestamptz, returned_at timestamptz",
    },
    "marketing": {
        "campaign": "name text, channel text, budget numeric(12,2), starts_on date, ends_on date",
        "campaign_customer": "campaign_id -> marketing.campaign, customer_id -> crm.customer, responded boolean",
        "ad_group": "campaign_id -> marketing.campaign, name text, daily_budget numeric(10,2)",
        "ad": "ad_group_id -> marketing.ad_group, headline text, impressions int, clicks int",
        "newsletter": "subject text, sent_on date, recipients int",
        "newsletter_open": "newsletter_id -> marketing.newsletter, contact_id -> crm.contact, opened_at timestamptz",
        "landing_page": "url text, campaign_id -> marketing.campaign, visits int, conversions int",
        "promo_code": "code text, campaign_id -> marketing.campaign, uses int",
        "social_post": "platform text, posted_at timestamptz, likes int, shares int",
        "influencer": "name text, platform text, followers int",
        "influencer_deal": "influencer_id -> marketing.influencer, campaign_id -> marketing.campaign, fee numeric(10,2)",
        "survey": "title text, sent_on date",
        "survey_response": "survey_id -> marketing.survey, contact_id -> crm.contact, score int, answered_at timestamptz",
        "segment": "name text, rule text",
        "segment_member": "segment_id -> marketing.segment, customer_id -> crm.customer",
    },
    "support": {
        "ticket": "customer_id -> crm.customer, subject text, priority text, status text, opened_at timestamptz, closed_at timestamptz",
        "ticket_comment": "ticket_id -> support.ticket, author_id -> hr.employee, body text, posted_at timestamptz",
        "agent": "employee_id -> hr.employee, team text, active boolean",
        "ticket_assignment": "ticket_id -> support.ticket, agent_id -> support.agent, assigned_at timestamptz",
        "sla_policy": "name text, response_hours int, resolution_hours int",
        "ticket_sla": "ticket_id -> support.ticket, sla_policy_id -> support.sla_policy, breached boolean",
        "knowledge_article": "title text, body text, views int, published_on date",
        "chat_session": "customer_id -> crm.customer, agent_id -> support.agent, started_at timestamptz, rating int",
        "chat_message": "chat_session_id -> support.chat_session, sender text, body text, sent_at timestamptz",
        "escalation": "ticket_id -> support.ticket, level int, escalated_at timestamptz",
        "csat_score": "ticket_id -> support.ticket, score int, submitted_at timestamptz",
        "macro": "name text, body text",
        "ticket_tag": "ticket_id -> support.ticket, tag text",
        "warranty_claim": "customer_id -> crm.customer, item_id -> product.item, filed_on date, status text",
        "callback_request": "customer_id -> crm.customer, requested_at timestamptz, handled boolean",
    },
    "analytics": {
        "daily_sales": "sales_date date, store_id -> sales.store, revenue numeric(12,2), orders int",
        "monthly_sales": "month date, revenue numeric(12,2), orders int, customers int",
        "customer_revenue": "customer_id -> crm.customer, total_revenue numeric(12,2), orders int, last_order_on date",
        "product_performance": "item_id -> product.item, month date, units_sold int, revenue numeric(12,2)",
        "store_revenue": "store_id -> sales.store, month date, revenue numeric(12,2)",
        "cohort_retention": "cohort_month date, months_since int, retained_customers int",
        "funnel_step": "funnel text, step int, users int, recorded_on date",
        "web_session": "visitor_id text, started_at timestamptz, pages int, converted boolean",
        "page_view": "web_session_id -> analytics.web_session, url text, viewed_at timestamptz",
        "kpi_snapshot": "kpi text, value numeric(14,4), snapshot_date date",
        "churn_prediction": "customer_id -> crm.customer, probability numeric(5,4), scored_on date",
        "category_sales": "category text, month date, revenue numeric(12,2)",
        "top_customers": "customer_id -> crm.customer, rank int, revenue numeric(12,2), period text",
        "rental_stats": "month date, rentals int, late_returns int",
        "genre_popularity": "genre text, month date, plays int",
    },
    "royalty": {
        "artist_contract": "artist_name text, label text, signed_on date, royalty_rate numeric(5,2)",
        "artist_payout": "artist_contract_id -> royalty.artist_contract, period date, amount numeric(12,2)",
        "track_license": "track_title text, licensee text, fee numeric(10,2), licensed_on date",
        "streaming_report": "platform text, period date, streams bigint, revenue numeric(12,2)",
        "streaming_report_line": "streaming_report_id -> royalty.streaming_report, track_title text, streams int",
        "publisher": "name text, country text",
        "songwriter": "first_name text, last_name text, publisher_id -> royalty.publisher",
        "composition": "title text, songwriter_id -> royalty.songwriter, iswc text",
        "recording": "composition_id -> royalty.composition, isrc text, duration_seconds int",
        "release_rights": "recording_id -> royalty.recording, territory text, starts_on date",
        "mechanical_royalty": "recording_id -> royalty.recording, period date, amount numeric(10,2)",
        "sync_deal": "recording_id -> royalty.recording, licensee text, fee numeric(10,2)",
        "concert": "artist_name text, venue text, city text, performed_on date",
        "ticket_sale": "concert_id -> royalty.concert, quantity int, amount numeric(10,2), sold_at timestamptz",
        "merch_sale": "artist_name text, item text, quantity int, amount numeric(10,2), sold_on date",
    },
    "logistics": {
        "carrier": "name text, country text",
        "route": "origin_city text, destination_city text, distance_km int",
        "vehicle": "plate text, kind text, capacity_kg int",
        "driver": "employee_id -> hr.employee, license_number text",
        "delivery": "shipment_reference text, route_id -> logistics.route, driver_id -> logistics.driver, delivered_at timestamptz",
        "delivery_stop": "delivery_id -> logistics.delivery, address text, sequence int",
        "fuel_log": "vehicle_id -> logistics.vehicle, liters numeric(8,2), cost numeric(10,2), filled_on date",
        "maintenance": "vehicle_id -> logistics.vehicle, description text, cost numeric(10,2), serviced_on date",
        "freight_invoice": "carrier_id -> logistics.carrier, amount numeric(12,2), issued_on date",
        "customs_declaration": "delivery_id -> logistics.delivery, country text, duty numeric(10,2)",
        "parcel": "delivery_id -> logistics.delivery, weight_kg numeric(8,2), tracking_code text",
        "return_label": "parcel_id -> logistics.parcel, issued_on date",
        "depot": "name text, city text, country text",
        "depot_stock": "depot_id -> logistics.depot, item_id -> product.item, quantity int",
        "incident": "delivery_id -> logistics.delivery, kind text, reported_at timestamptz",
    },
    "legacy": {
        "customer_2019": "first_name text, last_name text, email text, country text, created_on date",
        "payment_archive": "customer_ref int, amount numeric(10,2), payment_date timestamptz, method text",
        "rental_archive": "customer_ref int, film_title text, rented_on date, returned_on date",
        "invoice_archive": "customer_ref int, invoice_date date, total numeric(10,2), billing_country text",
        "employee_old": "first_name text, last_name text, title text, hired_on date",
        "order_import": "raw_customer text, raw_total text, imported_at timestamptz",
        "store_old": "name text, address text, manager text",
        "film_catalog_2018": "title text, rating text, rental_rate numeric(4,2), length int",
        "track_import": "title text, artist text, album text, genre text, milliseconds int",
        "album_import": "title text, artist text, released_on date",
        "actor_import": "full_name text, film_count int",
        "price_history": "item_name text, price numeric(10,2), changed_on date",
        "address_book": "name text, address text, city text, country text, phone text",
        "staff_import": "full_name text, store_name text, email text",
        "playlist_export": "name text, track_count int, exported_on date",
    },
}


# Commas between columns, not the ones inside a type like numeric(12,2).
_COLUMN_SEPARATOR = re.compile(r",(?![^()]*\))")


def _column(spec: str) -> tuple[str, str, str | None]:
    """(name, SQL type, referenced table or None) from "name type" / "name -> schema.table"."""
    if "->" in spec:
        name, target = (part.strip() for part in spec.split("->"))
        return name, "integer", target
    name, sql_type = spec.strip().split(" ", 1)
    return name, sql_type, None


def _quote(qualified: str) -> str:
    return ".".join(f'"{part}"' for part in qualified.split("."))


def ddl() -> str:
    """CREATE statements for every distractor table; foreign keys are added after all tables."""
    statements = [f'CREATE SCHEMA IF NOT EXISTS "{schema}";' for schema in SCHEMAS]
    foreign_keys = []
    for schema, tables in SCHEMAS.items():
        for table, spec in tables.items():
            qualified = f"{schema}.{table}"
            columns = ['"id" serial PRIMARY KEY']
            for name, sql_type, target in map(_column, _COLUMN_SEPARATOR.split(spec)):
                columns.append(f'"{name}" {sql_type}')
                if target:
                    foreign_keys.append(
                        f'ALTER TABLE {_quote(qualified)} ADD FOREIGN KEY ("{name}") REFERENCES {_quote(target)} ("id");'
                    )
            columns.append('"created_at" timestamptz NOT NULL DEFAULT now()')
            statements.append(f"CREATE TABLE {_quote(qualified)} ({', '.join(columns)});")
    return "\n".join(statements + foreign_keys) + "\n"


def table_count() -> int:
    return sum(len(tables) for tables in SCHEMAS.values())
