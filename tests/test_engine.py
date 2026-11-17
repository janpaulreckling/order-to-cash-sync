from datetime import UTC, datetime
from decimal import Decimal

from o2c_sync.engine import ORDER_STREAM, STATUS_STREAM
from o2c_sync.errors import PermanentError
from o2c_sync.models import ErpOrderStatus, InvoiceStatus
from o2c_sync.state import StateStore
from tests.conftest import FakeCRM, FakeERP, opportunity

OPP_A = "006Hs00001AAAAAIAA"
OPP_B = "006Hs00001BBBBBIAA"
OPP_C = "006Hs00001CCCCCIAA"


def test_creates_orders_and_customers(make_engine) -> None:
    engine, erp = make_engine(FakeCRM([opportunity(OPP_A), opportunity(OPP_B)]))

    stats = engine.run()

    assert (stats.created, stats.failed) == (2, 0)
    assert set(erp.orders) == {OPP_A, OPP_B}
    assert len(erp.customers) == 1  # same account, upserted not duplicated


def test_second_run_is_a_no_op(make_engine) -> None:
    crm = FakeCRM([opportunity(OPP_A)])
    engine, erp = make_engine(crm)
    engine.run()

    stats = engine.run()

    assert (stats.created, stats.updated, stats.skipped) == (0, 0, 1)
    assert erp.writes == 1


def test_no_duplicate_when_local_state_is_lost(make_engine, mapper, tmp_path) -> None:
    from o2c_sync.engine import SyncEngine

    crm, erp = FakeCRM([opportunity(OPP_A)]), FakeERP()
    SyncEngine(crm, erp, mapper, StateStore(f"sqlite:///{tmp_path / 'a.db'}")).run()
    # Fresh, empty state DB: the engine must find the order in the ERP itself.
    stats = SyncEngine(crm, erp, mapper, StateStore(f"sqlite:///{tmp_path / 'b.db'}")).run()

    assert stats.created == 0 and stats.updated == 1
    assert len(erp.orders) == 1


def test_changed_draft_order_is_updated(make_engine) -> None:
    crm = FakeCRM([opportunity(OPP_A)])
    engine, erp = make_engine(crm)
    engine.run()

    crm.records = [opportunity(OPP_A, lines=[("TENT-2P", 5, 349.0)],
                               modified="2026-11-03T10:00:00.000+0000")]
    stats = engine.run()

    assert stats.updated == 1
    assert erp.orders[OPP_A]["order"].lines[0].quantity == 5


def test_change_after_erp_confirmation_is_a_conflict(make_engine, store) -> None:
    crm = FakeCRM([opportunity(OPP_A)])
    engine, erp = make_engine(crm)
    engine.run()
    erp.orders[OPP_A]["state"] = "sale"  # Finance confirmed it

    crm.records = [opportunity(OPP_A, lines=[("TENT-2P", 5, 349.0)],
                               modified="2026-11-03T10:00:00.000+0000")]
    stats = engine.run()

    assert stats.failed == 1
    [dead] = store.open_dead_letters()
    assert dead.stage == "conflict" and "locked" in dead.error
    tent = next(ln for ln in erp.orders[OPP_A]["order"].lines if ln.sku == "TENT-2P")
    assert tent.quantity == 2  # ERP untouched


def test_bad_records_are_parked_and_do_not_block_others(make_engine, store) -> None:
    crm = FakeCRM([
        opportunity(OPP_A, modified="2026-11-01T08:00:00.000+0000"),
        opportunity(OPP_B, amount=1.0, modified="2026-11-01T09:00:00.000+0000"),  # mismatch
        opportunity(OPP_C, lines=[("KAYAK-X", 1, 900.0)],
                    modified="2026-11-01T10:00:00.000+0000"),  # unknown SKU
    ])
    engine, erp = make_engine(crm)

    stats = engine.run()

    assert (stats.created, stats.failed) == (1, 2)
    stages = {d.source_id: d.stage for d in store.open_dead_letters()}
    assert stages == {OPP_B: "validate", OPP_C: "load"}
    # Watermark moved past the poison records.
    assert store.get_watermark(ORDER_STREAM) == datetime(2026, 11, 1, 10, tzinfo=UTC)


def test_repeated_failure_updates_one_dead_letter(make_engine, store) -> None:
    crm = FakeCRM([opportunity(OPP_C, lines=[("KAYAK-X", 1, 900.0)])])
    engine, _ = make_engine(crm)
    engine.run()
    engine.run()

    [dead] = store.open_dead_letters()
    assert dead.attempts == 2


def test_replay_resolves_after_master_data_fix(make_engine, store) -> None:
    crm = FakeCRM([opportunity(OPP_C, lines=[("KAYAK-X", 1, 900.0)])])
    engine, erp = make_engine(crm)
    engine.run()

    erp.skus.add("KAYAK-X")  # product created in ERP
    stats = engine.replay_dead_letters()

    assert stats.created == 1
    assert store.open_dead_letters() == []


def test_rate_limit_aborts_but_keeps_progress(make_engine, store) -> None:
    crm = FakeCRM(
        [
            opportunity(OPP_A, modified="2026-11-01T08:00:00.000+0000"),
            opportunity(OPP_B, modified="2026-11-01T09:00:00.000+0000"),
        ],
        rate_limit_after=1,
    )
    engine, erp = make_engine(crm)

    stats = engine.run()

    assert stats.aborted and stats.created == 1
    assert store.get_watermark(ORDER_STREAM) == datetime(2026, 11, 1, 8, tzinfo=UTC)
    assert store.recent_runs(1)[0].status == "aborted"

    crm.rate_limit_after = None
    stats = engine.run()
    assert (stats.created, stats.skipped) == (1, 1)


def test_dry_run_writes_nothing(make_engine, store) -> None:
    engine, erp = make_engine(FakeCRM([opportunity(OPP_A)]))

    engine.run(dry_run=True)

    assert erp.writes == 0
    assert store.get_watermark(ORDER_STREAM) is None


def test_writeback_pushes_status_once(make_engine, store) -> None:
    crm = FakeCRM([])
    engine, erp = make_engine(crm)
    paid = ErpOrderStatus(external_id=OPP_A, erp_order_ref="S00001",
                          invoice_status=InvoiceStatus.PAID, amount_due=Decimal("0.00"),
                          updated_at=datetime(2026, 11, 5, 12, tzinfo=UTC))
    erp.statuses = [paid]

    engine.run()
    engine.run()  # same status again: no duplicate API call

    assert crm.status_updates == [paid]
    assert store.get_watermark(STATUS_STREAM) == paid.updated_at


def _status(opp: str, ref: str, minute: int, status: InvoiceStatus = InvoiceStatus.PAID,
            due: str = "0.00") -> ErpOrderStatus:
    return ErpOrderStatus(external_id=opp, erp_order_ref=ref, invoice_status=status,
                          amount_due=Decimal(due),
                          updated_at=datetime(2026, 11, 5, 12, minute, tzinfo=UTC))


class FlakyCRM(FakeCRM):
    """Write-back fails for the listed Opportunity Ids."""

    def __init__(self, records, failing: set[str]):  # type: ignore[no-untyped-def]
        super().__init__(records)
        self.failing = failing

    def update_order_status(self, status: ErpOrderStatus) -> None:
        if status.external_id in self.failing:
            raise PermanentError("FIELD_CUSTOM_VALIDATION_EXCEPTION")
        super().update_order_status(status)


def test_writeback_failure_does_not_overwrite_parked_order(make_engine, store) -> None:
    # OPP_A is parked as a conflict; then its write-back fails too.
    crm = FlakyCRM([opportunity(OPP_A)], failing={OPP_A})
    engine, erp = make_engine(crm)
    engine.run(writeback=False)
    erp.orders[OPP_A]["state"] = "sale"
    crm.records = [opportunity(OPP_A, lines=[("TENT-2P", 5, 349.0)],
                               modified="2026-11-03T10:00:00.000+0000")]
    erp.statuses = [_status(OPP_A, "S00001", 0)]

    engine.run()

    lanes = {d.stage: d for d in store.open_dead_letters()}
    assert set(lanes) == {"conflict", "writeback"}
    assert lanes["conflict"].payload["Id"] == OPP_A  # CRM payload kept for review
    assert engine.replay_dead_letters().failed == 1  # conflict still replayable


def test_failed_writeback_is_retried_on_next_run(make_engine, store) -> None:
    crm = FlakyCRM([], failing={OPP_A})
    engine, erp = make_engine(crm)
    first, second = _status(OPP_A, "S00001", 0), _status(OPP_B, "S00002", 5)
    erp.statuses = [first, second]

    engine.run()
    # B still goes through, but the watermark stays before A's failure.
    assert crm.status_updates == [second]
    assert store.get_watermark(STATUS_STREAM) is None

    crm.failing.clear()  # root cause fixed in Salesforce
    engine.run()

    assert crm.status_updates == [second, first]  # A retried, B not pushed twice
    assert store.get_watermark(STATUS_STREAM) == second.updated_at
    assert store.open_dead_letters() == []


def test_order_success_keeps_writeback_dead_letter_open(make_engine, store) -> None:
    crm = FlakyCRM([opportunity(OPP_A)], failing={OPP_A})
    engine, erp = make_engine(crm)
    erp.statuses = [_status(OPP_A, "S00001", 0)]
    engine.run()  # order created, write-back parked

    crm.records = [opportunity(OPP_A, lines=[("TENT-2P", 3, 349.0)],
                               modified="2026-11-03T10:00:00.000+0000")]
    engine.run(writeback=False)  # order updated successfully

    [dead] = store.open_dead_letters()
    assert dead.stage == "writeback"


def test_line_order_from_salesforce_does_not_matter(make_engine) -> None:
    # Found live: the child query has no guaranteed order, which used to change
    # the hash and park a confirmed order as a false conflict.
    crm = FakeCRM([opportunity(OPP_A, lines=[("TENT-2P", 2, 349.0), ("PACK-45L", 1, 189.0)])])
    engine, erp = make_engine(crm)
    engine.run()
    erp.orders[OPP_A]["state"] = "sale"
    crm.records = [opportunity(OPP_A, lines=[("PACK-45L", 1, 189.0), ("TENT-2P", 2, 349.0)],
                               modified="2026-11-03T10:00:00.000+0000")]

    stats = engine.run()

    assert (stats.skipped, stats.failed) == (1, 0)


def test_cosmetic_change_on_locked_order_is_not_a_conflict(make_engine, store) -> None:
    crm = FakeCRM([opportunity(OPP_A)])
    engine, erp = make_engine(crm)
    engine.run()
    erp.orders[OPP_A]["state"] = "sale"
    renamed = opportunity(OPP_A, modified="2026-11-03T10:00:00.000+0000")
    renamed["Name"] = "Renamed by the rep"
    crm.records = [renamed]

    stats = engine.run()
    again = engine.run()  # hash was re-baselined: nothing to do

    assert (stats.skipped, stats.failed) == (1, 0)
    assert store.open_dead_letters() == []
    assert erp.writes == 1 and again.failed == 0
