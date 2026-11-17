"""Order-to-cash analytics: extract from the live systems, model in SQL, render a dashboard.

    uv sync --extra analytics                   # DuckDB is not needed by the sync itself
    uv run python analytics/build.py            # extract + models + dashboard
    uv run python analytics/build.py --no-extract   # re-run models on the last extract

Extract (E/L): Salesforce REST, Odoo XML-RPC and the sync state DB land unchanged
in DuckDB's `raw` schema. Transform (T): every file in analytics/models runs in
name order, staging views first, then marts. Same layering as a dbt project,
without the tooling, so a reviewer can read the whole pipeline in one sitting.
"""

import argparse
import json
import xmlrpc.client
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import create_engine, text

from o2c_sync.config import Settings
from o2c_sync.connectors.salesforce import sf_cli_session

HERE = Path(__file__).resolve().parent
WAREHOUSE = HERE / "warehouse.duckdb"
MODELS = HERE / "models"
TEMPLATE = HERE / "dashboard_template.html"
OUTPUT = HERE / "dashboard.html"

SF_SOQL = """
SELECT Id, Name, AccountId, Account.Name, StageName, IsWon, IsClosed, Amount,
       CloseDate, CreatedDate, ERP_Order_Ref__c, ERP_Invoice_Status__c, ERP_Amount_Due__c
  FROM Opportunity
"""


# ------------------------------------------------------------------ extract
def _m2o(value: Any) -> Any:
    """Odoo many2one -> display name (False -> None)."""
    return value[1] if value else None


def _none(value: Any) -> Any:
    return None if value is False else value


def extract_salesforce(s: Settings) -> list[tuple[Any, ...]]:
    if not s.sf_cli_org:
        raise SystemExit("set O2C_SF_CLI_ORG to extract from Salesforce")
    sf = sf_cli_session(s.sf_cli_org)
    return [
        (r["Id"], r["Name"], r["AccountId"], (r["Account"] or {}).get("Name"), r["StageName"],
         r["IsWon"], r["IsClosed"], r["Amount"], r["CloseDate"], r["CreatedDate"][:19],
         r["ERP_Order_Ref__c"], r["ERP_Invoice_Status__c"], r["ERP_Amount_Due__c"])
        for r in sf.query_all(SF_SOQL)["records"]
    ]


def extract_odoo(s: Settings) -> dict[str, list[tuple[Any, ...]]]:
    pw = s.odoo_password.get_secret_value()
    common = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/common", allow_none=True)
    models = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/object", allow_none=True)
    uid = common.authenticate(s.odoo_db, s.odoo_username, pw, {})

    def read(model: str, domain: list[Any], fields: list[str]) -> list[dict[str, Any]]:
        rows: Any = models.execute_kw(
            s.odoo_db, uid, pw, model, "search_read", [domain], {"fields": fields})
        return list(rows)

    orders = read("sale.order", [], ["id", "name", "client_order_ref", "partner_id", "state",
                                     "amount_total", "date_order", "create_date"])
    moves = read("account.move", [("move_type", "in", ["out_invoice", "out_refund"])],
                 ["id", "name", "move_type", "state", "invoice_origin", "partner_id",
                  "invoice_date", "invoice_date_due", "amount_total", "amount_residual",
                  "payment_state", "reversed_entry_id", "invoice_payment_term_id"])
    payments = read("account.payment", [("payment_type", "=", "inbound")],
                    ["id", "name", "date", "amount", "partner_id", "state",
                     "reconciled_invoice_ids"])
    return {
        "odoo_sale_orders": [
            (o["id"], o["name"], _none(o["client_order_ref"]), _m2o(o["partner_id"]), o["state"],
             o["amount_total"], o["date_order"], o["create_date"]) for o in orders],
        "odoo_moves": [
            (m["id"], m["name"], m["move_type"], m["state"], _none(m["invoice_origin"]),
             _m2o(m["partner_id"]), _none(m["invoice_date"]), _none(m["invoice_date_due"]),
             m["amount_total"], m["amount_residual"], m["payment_state"],
             m["reversed_entry_id"][0] if m["reversed_entry_id"] else None,
             _m2o(m["invoice_payment_term_id"])) for m in moves],
        "odoo_payments": [
            (p["id"], p["name"], p["date"], p["amount"], _m2o(p["partner_id"]), p["state"])
            for p in payments],
        "odoo_payment_invoices": [
            (p["id"], inv_id) for p in payments for inv_id in p["reconciled_invoice_ids"]],
    }


def extract_state(s: Settings) -> dict[str, list[tuple[Any, ...]]]:
    engine = create_engine(s.database_url)
    with engine.connect() as conn:
        runs = conn.execute(text("SELECT id, started_at, finished_at, status, stats, note "
                                 "FROM sync_run")).all()
        dead = conn.execute(text("SELECT id, source_id, stage, error, attempts, created_at, "
                                 "last_seen_at, resolved_at FROM dead_letter")).all()

    def stats(raw: Any) -> dict[str, Any]:
        return (json.loads(raw) if isinstance(raw, str) else raw) or {}

    return {
        "sync_runs": [
            (r.id, r.started_at, r.finished_at, r.status, stats(r.stats).get("created", 0),
             stats(r.stats).get("updated", 0), stats(r.stats).get("skipped", 0),
             stats(r.stats).get("failed", 0), stats(r.stats).get("written_back", 0), r.note)
            for r in runs],
        "dead_letters": [tuple(r) for r in dead],
    }


RAW_DDL = {
    "sf_opportunities": "id VARCHAR, name VARCHAR, account_id VARCHAR, account_name VARCHAR, "
                        "stage VARCHAR, is_won BOOLEAN, is_closed BOOLEAN, amount DECIMAL(18,2), "
                        "close_date DATE, created_at TIMESTAMP, erp_order_ref VARCHAR, "
                        "erp_invoice_status VARCHAR, erp_amount_due DECIMAL(18,2)",
    "odoo_sale_orders": "id INTEGER, name VARCHAR, client_order_ref VARCHAR, partner_name VARCHAR, "
                        "state VARCHAR, amount_total DECIMAL(18,2), date_order TIMESTAMP, "
                        "create_date TIMESTAMP",
    "odoo_moves": "id INTEGER, name VARCHAR, move_type VARCHAR, state VARCHAR, "
                  "invoice_origin VARCHAR, partner_name VARCHAR, invoice_date DATE, "
                  "invoice_date_due DATE, amount_total DECIMAL(18,2), "
                  "amount_residual DECIMAL(18,2), payment_state VARCHAR, "
                  "reversed_entry_id INTEGER, payment_term VARCHAR",
    "odoo_payments": "id INTEGER, name VARCHAR, payment_date DATE, amount DECIMAL(18,2), "
                     "partner_name VARCHAR, state VARCHAR",
    "odoo_payment_invoices": "payment_id INTEGER, move_id INTEGER",
    "sync_runs": "id INTEGER, started_at TIMESTAMP, finished_at TIMESTAMP, status VARCHAR, "
                 "created INTEGER, updated INTEGER, skipped INTEGER, failed INTEGER, "
                 "written_back INTEGER, note VARCHAR",
    "dead_letters": "id INTEGER, source_id VARCHAR, stage VARCHAR, error VARCHAR, "
                    "attempts INTEGER, created_at TIMESTAMP, last_seen_at TIMESTAMP, "
                    "resolved_at TIMESTAMP",
}


def load_raw(con: duckdb.DuckDBPyConnection, tables: dict[str, Iterable[Sequence[Any]]]) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    for name, rows in tables.items():
        rows = list(rows)
        con.execute(f"CREATE OR REPLACE TABLE raw.{name} ({RAW_DDL[name]})")
        if rows:
            marks = ", ".join("?" * len(rows[0]))
            con.executemany(f"INSERT INTO raw.{name} VALUES ({marks})", rows)
        print(f"  raw.{name}: {len(rows)} rows")


# ---------------------------------------------------------------- transform
def run_models(con: duckdb.DuckDBPyConnection) -> None:
    for layer in ("staging", "marts"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")
        for sql_file in sorted((MODELS / layer).glob("*.sql")):
            con.execute(sql_file.read_text())
            print(f"  {layer}.{sql_file.stem.split('_', 1)[1]}")


# ---------------------------------------------------------------- dashboard
def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(type(value))


def query(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def render_dashboard(con: duckdb.DuckDBPyConnection) -> None:
    data = {
        "as_of": date.today().isoformat(),
        "kpis": query(con, "SELECT * FROM marts.kpis")[0],
        "monthly": query(con, "SELECT * FROM marts.monthly_cash_flow ORDER BY month"),
        "aging": query(con, "SELECT * FROM marts.ar_aging ORDER BY bucket_order"),
        "customers": query(con, "SELECT * FROM marts.customer_payment_behavior "
                                "ORDER BY avg_days_vs_terms DESC NULLS LAST"),
        "cycle": query(con, "SELECT * FROM marts.cycle_time_by_month ORDER BY month"),
        "recon": query(con, "SELECT * FROM marts.crm_erp_reconciliation "
                            "WHERE check_status <> 'ok' ORDER BY close_date"),
        "recon_summary": query(con, "SELECT check_status, COUNT(*) AS deals, "
                                    "SUM(crm_amount) AS amount FROM marts.crm_erp_reconciliation "
                                    "GROUP BY 1 ORDER BY 2 DESC"),
        "dead_letters": query(con, "SELECT * FROM marts.sync_health_dead_letters"),
    }
    payload = json.dumps(data, default=_json_default).replace("</", "<\\/")
    OUTPUT.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", payload))
    print(f"dashboard: {OUTPUT.relative_to(HERE.parent)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-extract", action="store_true", help="reuse the last extract")
    args = parser.parse_args()

    con = duckdb.connect(str(WAREHOUSE))
    if not args.no_extract:
        s = Settings()
        print("extract:")
        load_raw(con, {"sf_opportunities": extract_salesforce(s), **extract_odoo(s),
                       **extract_state(s)})
    print("models:")
    run_models(con)
    render_dashboard(con)


if __name__ == "__main__":
    main()
