from datetime import UTC, datetime
from decimal import Decimal

import pytest

from o2c_sync.errors import MappingError
from o2c_sync.mapping import FieldMapper
from tests.conftest import opportunity


def test_maps_salesforce_record_to_canonical_order(mapper: FieldMapper) -> None:
    order = mapper.to_sales_order(opportunity())

    assert order.external_id == "006Hs00001AbCdEIAA"
    assert order.currency == "USD"  # from defaults
    assert order.customer.billing_address.country_code == "US"  # "United States" -> US
    assert order.source_last_modified == datetime(2026, 11, 2, 14, 3, 11, tzinfo=UTC)
    assert [line.sku for line in order.lines] == ["TENT-2P", "PACK-45L"]
    assert order.total == Decimal("887.00")


def test_missing_discount_falls_back_to_zero(mapper: FieldMapper) -> None:
    order = mapper.to_sales_order(opportunity())
    assert all(line.discount_pct == 0 for line in order.lines)


def test_rejects_order_without_line_items(mapper: FieldMapper) -> None:
    with pytest.raises(MappingError, match="lines"):
        mapper.to_sales_order(opportunity(lines=[], amount=500.0))


def test_rejects_amount_mismatch(mapper: FieldMapper) -> None:
    with pytest.raises(MappingError, match="CRM amount is 999"):
        mapper.to_sales_order(opportunity(amount=999.0))


def test_rejects_unknown_country(mapper: FieldMapper) -> None:
    with pytest.raises(MappingError, match="unknown country"):
        mapper.to_sales_order(opportunity(country="Atlantis"))


def test_source_modified_survives_invalid_record(mapper: FieldMapper) -> None:
    bad = opportunity(lines=[], amount=1.0)
    assert mapper.source_modified(bad) == datetime(2026, 11, 2, 14, 3, 11, tzinfo=UTC)


def test_fingerprint_ignores_timestamp_but_not_content(mapper: FieldMapper) -> None:
    a = mapper.to_sales_order(opportunity(modified="2026-11-02T14:03:11.000+0000"))
    b = mapper.to_sales_order(opportunity(modified="2026-11-03T09:00:00.000+0000"))
    c = mapper.to_sales_order(opportunity(lines=[("TENT-2P", 3, 349.0)]))
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_unknown_transform_is_a_config_error() -> None:
    spec = {
        "order": {"external_id": {"path": "Id", "transform": "nope"}},
        "customer": {}, "billing_address": {}, "line_items": {"path": "x", "fields": {}},
    }
    with pytest.raises(ValueError, match="unknown transform"):
        FieldMapper(spec)
