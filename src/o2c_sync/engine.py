"""Sync orchestration: CRM -> ERP (orders) and ERP -> CRM (financial status)."""

import logging
from enum import StrEnum
from typing import Any

from o2c_sync.connectors.base import CRMSource, ERPTarget
from o2c_sync.errors import (
    ConflictError,
    MappingError,
    PermanentError,
    RateLimitExceeded,
    TransientError,
)
from o2c_sync.mapping import FieldMapper
from o2c_sync.models import SalesOrder
from o2c_sync.state import RunStats, StateStore

log = logging.getLogger(__name__)

ORDER_STREAM = "crm.closed_won"
STATUS_STREAM = "erp.order_status"


class Outcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"


class SyncEngine:
    def __init__(
        self, crm: CRMSource, erp: ERPTarget, mapper: FieldMapper, store: StateStore
    ) -> None:
        self.crm, self.erp, self.mapper, self.store = crm, erp, mapper, store

    # ------------------------------------------------------------------ run
    def run(self, *, dry_run: bool = False, writeback: bool = True) -> RunStats:
        run_id = self.store.start_run()
        stats = RunStats()
        note = None
        try:
            self._sync_orders(run_id, stats, dry_run)
            if writeback and not dry_run:
                self._sync_statuses(run_id, stats)
        except RateLimitExceeded as exc:
            # Progress made so far is already checkpointed; the next scheduled
            # run resumes from the watermark.
            stats.aborted = True
            note = f"aborted: {exc}"
            log.warning(note)
        finally:
            self.store.finish_run(run_id, stats, note)
        return stats

    # ---------------------------------------------------------- CRM -> ERP
    def _sync_orders(self, run_id: int, stats: RunStats, dry_run: bool) -> None:
        since = self.store.get_watermark(ORDER_STREAM)
        log.info("syncing won opportunities modified since %s", since or "the beginning")

        for raw in self.crm.fetch_closed_won(since):
            source_id = self.mapper.source_id(raw)
            try:
                order = self.mapper.to_sales_order(raw)
                if dry_run:
                    log.info("[dry-run] would sync %s (%s, %s)", source_id, order.name, order.total)
                    stats.skipped += 1
                    continue
                self._record(stats, self.push_order(order))
                self.store.resolve_dead_letters(source_id)
            except (PermanentError, TransientError) as exc:
                # TransientError here means retries were already exhausted.
                stage = self._stage_for(exc)
                log.error("%s failed at %s: %s", source_id, stage, exc)
                if not dry_run:
                    self.store.dead_letter(run_id, source_id, stage, str(exc), _jsonable(raw))
                stats.failed += 1
                stats.errors.append(f"{source_id}: {exc}")

            # Checkpoint after every record, successful or parked, so an abort
            # never replays work and a poison record never blocks the stream.
            modified = self.mapper.source_modified(raw)
            if modified and not dry_run:
                self.store.set_watermark(ORDER_STREAM, modified)

    def push_order(self, order: SalesOrder) -> Outcome:
        """Idempotent upsert of one order into the ERP."""
        fingerprint = order.fingerprint()
        known = self.store.get_mapping("order", order.external_id)
        if known and known.payload_hash == fingerprint:
            return Outcome.SKIPPED

        customer_ref = self.erp.upsert_customer(order.customer)
        # Look up by external id in the ERP itself, not only in our id map:
        # if local state is lost, we still must not create a duplicate order.
        existing = self.erp.find_order(order.external_id)

        if existing is None:
            ref = self.erp.create_order(order, customer_ref)
            outcome = Outcome.CREATED
        elif existing.editable:
            self.erp.update_order(existing.ref, order, customer_ref)
            ref, outcome = existing.ref, Outcome.UPDATED
        else:
            # Conflict rule: once Finance confirms an order in the ERP, the ERP
            # is the system of record. CRM edits after that point need review.
            raise ConflictError(
                f"ERP order {existing.name} is '{existing.state}' and locked; "
                "CRM changes after confirmation require manual review"
            )

        self.store.save_mapping("order", order.external_id, ref, fingerprint)
        log.info("%s %s -> ERP %s", outcome, order.external_id, ref)
        return outcome

    # ---------------------------------------------------------- ERP -> CRM
    def _sync_statuses(self, run_id: int, stats: RunStats) -> None:
        since = self.store.get_watermark(STATUS_STREAM)
        for status in self.erp.list_status_changes(since):
            fingerprint = status.fingerprint()
            known = self.store.get_mapping("order_status", status.external_id)
            if not (known and known.payload_hash == fingerprint):
                try:
                    self.crm.update_order_status(status)
                    self.store.save_mapping(
                        "order_status", status.external_id, status.erp_order_ref, fingerprint
                    )
                    stats.written_back += 1
                except (PermanentError, TransientError) as exc:
                    self.store.dead_letter(
                        run_id, status.external_id, "writeback", str(exc),
                        status.model_dump(mode="json"),
                    )
                    stats.failed += 1
                    stats.errors.append(f"{status.external_id}: {exc}")
            self.store.set_watermark(STATUS_STREAM, status.updated_at)

    # ------------------------------------------------------------ replay
    def replay_dead_letters(self) -> RunStats:
        """Retry parked orders from their stored payload, e.g. after the missing
        product was created in the ERP."""
        run_id = self.store.start_run()
        stats = RunStats()
        try:
            for entry in self.store.open_dead_letters():
                if entry.stage == "writeback":
                    continue  # regenerated from ERP on the next normal run
                try:
                    order = self.mapper.to_sales_order(entry.payload)
                    self._record(stats, self.push_order(order))
                    self.store.resolve_dead_letters(entry.source_id)
                except (PermanentError, TransientError) as exc:
                    self.store.dead_letter(
                        run_id, entry.source_id, self._stage_for(exc), str(exc), entry.payload
                    )
                    stats.failed += 1
        except RateLimitExceeded:
            stats.aborted = True
        finally:
            self.store.finish_run(run_id, stats, "dlq replay")
        return stats

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _record(stats: RunStats, outcome: Outcome) -> None:
        if outcome is Outcome.CREATED:
            stats.created += 1
        elif outcome is Outcome.UPDATED:
            stats.updated += 1
        else:
            stats.skipped += 1

    @staticmethod
    def _stage_for(exc: Exception) -> str:
        if isinstance(exc, MappingError):
            return "validate"
        if isinstance(exc, ConflictError):
            return "conflict"
        return "load"


def _jsonable(raw: Any) -> Any:
    """Salesforce returns OrderedDicts; normalise for JSON storage."""
    if isinstance(raw, dict):
        return {k: _jsonable(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return [_jsonable(v) for v in raw]
    return raw
