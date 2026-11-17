"""Config-driven mapping from raw Salesforce records to the canonical model."""

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from o2c_sync.errors import MappingError
from o2c_sync.models import Address, Customer, OrderLine, SalesOrder

Record = Mapping[str, Any]

_COUNTRY_NAMES = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "canada": "CA",
    "mexico": "MX",
    "united kingdom": "GB",
    "uk": "GB",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
}


def _country_iso2(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 2:
        return text.upper()
    try:
        return _COUNTRY_NAMES[text.lower()]
    except KeyError:
        raise MappingError(f"unknown country {text!r}; add it to the country table") from None


def _sf_datetime(value: Any) -> str | None:
    # Salesforce sends "+0000"; ISO 8601 parsers want "+00:00".
    if value is None:
        return None
    return re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(value))


TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "country_iso2": _country_iso2,
    "sf_datetime": _sf_datetime,
    "upper": lambda v: None if v is None else str(v).upper(),
}


def resolve(record: Any, path: str) -> Any:
    """Follow a dotted path through nested dicts; missing parts yield None."""
    current = record
    for part in path.split("."):
        if current is None:
            return None
        if not isinstance(current, Mapping):
            raise MappingError(f"cannot descend into {part!r} of path {path!r}")
        current = current.get(part)
    return current


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'record'}: {err['msg']}"
        for err in exc.errors()
    )


class FieldMapper:
    REQUIRED_SECTIONS = ("order", "customer", "billing_address", "line_items")

    def __init__(self, spec: Mapping[str, Any]) -> None:
        missing = [s for s in self.REQUIRED_SECTIONS if s not in spec]
        if missing:
            raise ValueError(f"mapping spec is missing sections: {missing}")
        for section in ("order", "customer", "billing_address"):
            self._check_transforms(spec[section])
        self._check_transforms(spec["line_items"]["fields"])
        self._spec = spec
        self._defaults: Mapping[str, Any] = spec.get("defaults", {})

    @classmethod
    def from_yaml(cls, path: Path) -> "FieldMapper":
        with path.open() as fh:
            return cls(yaml.safe_load(fh))

    @staticmethod
    def _check_transforms(fields: Mapping[str, Any]) -> None:
        for name, rule in fields.items():
            if isinstance(rule, Mapping) and rule.get("transform") not in TRANSFORMS:
                raise ValueError(f"field {name!r} uses unknown transform {rule.get('transform')!r}")

    @staticmethod
    def _map_fields(record: Any, fields: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, rule in fields.items():
            if isinstance(rule, Mapping):
                value = TRANSFORMS[rule["transform"]](resolve(record, rule["path"]))
            else:
                value = resolve(record, rule)
            if value is not None:  # let model defaults apply
                out[name] = value
        return out

    def source_id(self, record: Record) -> str:
        value = resolve(record, self._spec["order"]["external_id"])
        return str(value) if value else "<missing id>"

    def source_modified(self, record: Record) -> datetime | None:
        """Best-effort timestamp, available even when the record fails validation,
        so the watermark can still move past a bad record."""
        try:
            fields = self._map_fields(record, {"m": self._spec["order"]["source_last_modified"]})
            return datetime.fromisoformat(fields["m"]) if "m" in fields else None
        except (MappingError, ValueError):
            return None

    def to_sales_order(self, record: Record) -> SalesOrder:
        spec = self._spec
        try:
            order = self._map_fields(record, spec["order"])
            order.setdefault("currency", self._defaults.get("currency"))

            address = self._map_fields(record, spec["billing_address"])
            address.setdefault("country_code", self._defaults.get("country_code", "US"))
            customer = self._map_fields(record, spec["customer"])

            raw_lines = resolve(record, spec["line_items"]["path"]) or []
            lines = [
                OrderLine(**self._map_fields(raw, spec["line_items"]["fields"]))
                for raw in raw_lines
            ]
            return SalesOrder(
                **order,
                customer=Customer(**customer, billing_address=Address(**address)),
                lines=tuple(lines),
            )
        except ValidationError as exc:
            raise MappingError(_summarize(exc)) from exc
        except TypeError as exc:  # unexpected shapes, e.g. a string where a list belongs
            raise MappingError(str(exc)) from exc
