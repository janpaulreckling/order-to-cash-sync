"""The SQL models, run on a tiny hand-checkable raw layer (no live systems)."""

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from analytics.build import load_raw, run_models

TODAY = date.today()
D = lambda days_ago: TODAY - timedelta(days=days_ago)  # noqa: E731

OPP = {"A": "006A00000000000AAA", "B": "006B00000000000AAA", "C": "006C00000000000AAA",
       "D": "006D00000000000AAA", "L": "006L00000000000AAA"}


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    load_raw(con, {
        "sf_opportunities": [
            # id, name, account_id, account, stage, won, closed, amount, close, created,
            # erp ref, crm status, crm due
            (OPP["A"], "A paid late", "001", "Acme", "Closed Won", True, True, 1000, D(80), D(90),
             "S1", "Paid", 0),
            (OPP["B"], "B open", "001", "Acme", "Closed Won", True, True, 500, D(50), D(60),
             "S2", "Invoiced", 500),
            (OPP["C"], "C credited", "002", "Beta", "Closed Won", True, True, 300, D(40), D(45),
             "S3", "Credited", 0),
            (OPP["D"], "D missing", "002", "Beta", "Closed Won", True, True, 200, D(5), D(6),
             None, None, None),
            (OPP["L"], "L lost", "002", "Beta", "Closed Lost", False, True, 999, D(5), D(6),
             None, None, None),
        ],
        "odoo_sale_orders": [
            (1, "S1", OPP["A"], "Acme", "sale", 1000, D(79), D(80)),
            (2, "S2", OPP["B"], "Acme", "sale", 500, D(49), D(50)),
            (3, "S3", OPP["C"], "Beta", "sale", 300, D(39), D(40)),
        ],
        "odoo_moves": [
            # id, name, type, state, origin, partner, inv date, due, total, residual,
            # payment_state, reversed_entry_id, term
            (10, "INV/1", "out_invoice", "posted", "S1", "Acme", D(75), D(45), 1000, 0,
             "paid", None, "30 Days"),
            (11, "INV/2", "out_invoice", "posted", "S2", "Acme", D(45), D(15), 500, 500,
             "not_paid", None, "30 Days"),
            (12, "INV/3", "out_invoice", "posted", "S3", "Beta", D(35), D(5), 300, 0,
             "reversed", None, "30 Days"),
            (13, "RINV/1", "out_refund", "posted", "S3", "Beta", D(30), D(30), 300, 0,
             "paid", 12, None),
            (14, "INV/draft", "out_invoice", "draft", "S2", "Acme", None, None, 999, 999,
             "not_paid", None, None),
        ],
        "odoo_payments": [(20, "P1", D(40), 600, "Acme", "posted"),
                          (21, "P2", D(35), 400, "Acme", "posted")],
        "odoo_payment_invoices": [(20, 10), (21, 10)],
        "sync_runs": [(1, D(1), D(1), "partial", 3, 0, 0, 1, 3, None)],
        "dead_letters": [(1, OPP["D"], "load", "unknown SKU(s): X", 1, D(1), D(1), None)],
    })
    run_models(con)
    return con


def one(con: duckdb.DuckDBPyConnection, sql: str) -> tuple:  # type: ignore[type-arg]
    row = con.execute(sql).fetchone()
    assert row is not None
    return row


def test_order_to_cash_timeline(con) -> None:
    status, won_to_cash, vs_due, cash = one(con, f"""
        SELECT erp_status, days_won_to_cash, days_paid_vs_due, cash_received
        FROM marts.fct_order_to_cash WHERE opportunity_id = '{OPP["A"]}'""")
    # Paid in two installments; settled by the second payment, 10 days after due.
    assert (status, won_to_cash, vs_due, cash) == ("Paid", 45, 10, Decimal("1000.00"))


def test_credit_note_is_not_cash(con) -> None:
    status, net_billed, cash = one(con, f"""
        SELECT erp_status, net_billed, cash_received
        FROM marts.fct_order_to_cash WHERE opportunity_id = '{OPP["C"]}'""")
    assert (status, net_billed, cash) == ("Credited", Decimal("0.00"), Decimal("0.00"))


def test_lost_deals_and_drafts_are_excluded(con) -> None:
    deals, bookings, open_ar = one(con, "SELECT deals_won, bookings, open_ar FROM marts.kpis")
    assert (deals, bookings, open_ar) == (4, Decimal("2000.00"), Decimal("500.00"))


def test_ar_aging_buckets(con) -> None:
    rows = con.execute("SELECT bucket, invoices, amount_open FROM marts.ar_aging "
                       "ORDER BY bucket_order").fetchall()
    assert rows[1] == ("1-30 days", 1, Decimal("500.00"))  # INV/2, 15 days past due
    assert sum(r[1] for r in rows) == 1


def test_reconciliation_flags_missing_deal_with_reason(con) -> None:
    rows = con.execute("SELECT opportunity_name, check_status, dlq_reason "
                       "FROM marts.crm_erp_reconciliation WHERE check_status <> 'ok'").fetchall()
    assert rows == [("D missing", "missing in ERP", "load: unknown SKU(s): X")]


def test_monthly_cash_flow_nets_credit_notes(con) -> None:
    billed, collected = one(con, "SELECT SUM(billings), SUM(collections) "
                                 "FROM marts.monthly_cash_flow")
    assert (billed, collected) == (Decimal("1500.00"), Decimal("1000.00"))
