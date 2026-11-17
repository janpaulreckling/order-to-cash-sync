"""Persistent sync state: watermarks, ID map, run log and dead-letter queue.

SQLite locally, Postgres in docker-compose; same code via SQLAlchemy.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; everything we store is UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class Base(DeclarativeBase):
    pass


class Watermark(Base):
    __tablename__ = "sync_watermark"
    stream: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdMap(Base):
    """Source record -> target record, plus the hash of what we last wrote."""

    __tablename__ = "id_map"
    entity: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_ref: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncRun(Base):
    __tablename__ = "sync_run"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text)


class DeadLetter(Base):
    __tablename__ = "dead_letter"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_run.id"))
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(16))  # validate | load | conflict | writeback
    error: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@dataclass
class RunStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    written_back: int = 0
    aborted: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "written_back": self.written_back,
            "aborted": self.aborted,
        }


class StateStore:
    def __init__(self, url: str) -> None:
        self._engine = create_engine(url)
        Base.metadata.create_all(self._engine)
        self._session = sessionmaker(self._engine, expire_on_commit=False)

    def _tx(self) -> Session:
        return self._session.begin()  # type: ignore[return-value]

    # --- watermarks -------------------------------------------------------
    def get_watermark(self, stream: str) -> datetime | None:
        with self._session() as s:
            row = s.get(Watermark, stream)
            return _aware(row.value) if row else None

    def set_watermark(self, stream: str, value: datetime) -> None:
        with self._tx() as s:
            row = s.get(Watermark, stream)
            if row is None:
                s.add(Watermark(stream=stream, value=value))
            else:
                row.value, row.updated_at = value, utcnow()

    # --- id map -----------------------------------------------------------
    def get_mapping(self, entity: str, source_id: str) -> IdMap | None:
        with self._session() as s:
            return s.get(IdMap, (entity, source_id))

    def save_mapping(self, entity: str, source_id: str, target_ref: str, payload_hash: str) -> None:
        with self._tx() as s:
            row = s.get(IdMap, (entity, source_id))
            if row is None:
                s.add(IdMap(entity=entity, source_id=source_id,
                            target_ref=target_ref, payload_hash=payload_hash))
            else:
                row.target_ref, row.payload_hash, row.synced_at = target_ref, payload_hash, utcnow()

    # --- runs -------------------------------------------------------------
    def start_run(self) -> int:
        with self._tx() as s:
            run = SyncRun()
            s.add(run)
            s.flush()
            return run.id

    def finish_run(self, run_id: int, stats: RunStats, note: str | None = None) -> None:
        if stats.aborted:
            status = "aborted"
        elif stats.failed:
            status = "partial"
        else:
            status = "success"
        with self._tx() as s:
            run = s.get_one(SyncRun, run_id)
            run.finished_at, run.status = utcnow(), status
            run.stats, run.note = stats.as_dict(), note

    def recent_runs(self, limit: int = 10) -> Sequence[SyncRun]:
        with self._session() as s:
            return s.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)).all()

    # --- dead-letter queue -----------------------------------------------
    def dead_letter(
        self, run_id: int, source_id: str, stage: str, error: str, payload: dict[str, Any]
    ) -> None:
        """Park a failed record. Repeated failures of the same record update the
        open entry instead of piling up duplicates."""
        with self._tx() as s:
            row = s.scalars(
                select(DeadLetter).where(
                    DeadLetter.source_id == source_id, DeadLetter.resolved_at.is_(None)
                )
            ).first()
            if row is None:
                s.add(DeadLetter(run_id=run_id, source_id=source_id, stage=stage,
                                 error=error, payload=payload))
            else:
                row.run_id, row.stage, row.error, row.payload = run_id, stage, error, payload
                row.attempts += 1
                row.last_seen_at = utcnow()

    def resolve_dead_letters(self, source_id: str) -> int:
        with self._tx() as s:
            rows = s.scalars(
                select(DeadLetter).where(
                    DeadLetter.source_id == source_id, DeadLetter.resolved_at.is_(None)
                )
            ).all()
            for row in rows:
                row.resolved_at = utcnow()
            return len(rows)

    def open_dead_letters(self) -> Sequence[DeadLetter]:
        with self._session() as s:
            return s.scalars(
                select(DeadLetter).where(DeadLetter.resolved_at.is_(None)).order_by(DeadLetter.id)
            ).all()
