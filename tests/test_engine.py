from datetime import UTC, datetime
from decimal import Decimal

from o2c_sync.engine import ORDER_STREAM, STATUS_STREAM
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
    assert erp.orders[OPP_A]["order"].lines[0].quantity == 2  # ERP untouched


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
