"""Salesforce connector (REST API via simple-salesforce).

Write-back requires four custom fields on Opportunity; see docs/salesforce_setup.md.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar

import requests
from simple_salesforce.api import Salesforce
from simple_salesforce.exceptions import (
    SalesforceError,
    SalesforceExpiredSession,
    SalesforceGeneralError,
    SalesforceRefusedRequest,
)

from o2c_sync.errors import PermanentError, RateLimitExceeded, TransientError
from o2c_sync.models import ErpOrderStatus
from o2c_sync.retry import retry_policy

log = logging.getLogger(__name__)
T = TypeVar("T")

CLOSED_WON_SOQL = """
SELECT Id, Name, Amount, CloseDate, LastModifiedDate,
       Account.Id, Account.Name, Account.Phone,
       Account.BillingStreet, Account.BillingCity, Account.BillingState,
       Account.BillingPostalCode, Account.BillingCountry,
       (SELECT Quantity, UnitPrice, Discount, Product2.ProductCode, Product2.Name
          FROM OpportunityLineItems)
  FROM Opportunity
 WHERE IsWon = true{since_clause}
 ORDER BY LastModifiedDate ASC, Id ASC
"""


def _content(exc: SalesforceError) -> str:
    body = exc.content
    return body.decode(errors="replace") if isinstance(body, bytes) else str(body)


def soql_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SalesforceCRM:
    def __init__(
        self,
        username: str,
        password: str,
        security_token: str,
        domain: str = "login",
        api_usage_ceiling: float = 0.8,
        max_retries: int = 5,
    ) -> None:
        self._username, self._password = username, password
        self._token, self._domain = security_token, domain
        self._ceiling = api_usage_ceiling
        self._retry = retry_policy(max_retries)
        self._sf = self._login()

    def _login(self) -> Salesforce:
        return Salesforce(username=self._username, password=self._password,
                          security_token=self._token, domain=self._domain)

    # ------------------------------------------------------------ guards
    def _check_api_budget(self) -> None:
        usage = (getattr(self._sf, "api_usage", None) or {}).get("api-usage")
        if usage and usage.total and usage.used / usage.total >= self._ceiling:
            raise RateLimitExceeded(
                f"org API usage at {usage.used}/{usage.total} "
                f"(ceiling {self._ceiling:.0%}); yielding to other integrations"
            )

    def _call(self, fn: Callable[[], T]) -> T:
        """Run one API call with budget check, error translation and retries.

        `fn` must look up `self._sf` when invoked (use a lambda), so a retry
        after re-login uses the fresh session.
        """

        def attempt() -> T:
            self._check_api_budget()
            try:
                return fn()
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise TransientError(f"network: {exc}") from exc
            except SalesforceExpiredSession as exc:
                self._sf = self._login()
                raise TransientError("session expired; re-authenticated") from exc
            except SalesforceRefusedRequest as exc:
                if "REQUEST_LIMIT_EXCEEDED" in _content(exc):
                    raise RateLimitExceeded("Salesforce daily API limit reached") from exc
                raise PermanentError(f"refused: {_content(exc)}") from exc
            except SalesforceGeneralError as exc:
                if exc.status >= 500 or exc.status == 429:
                    raise TransientError(f"HTTP {exc.status}") from exc
                raise PermanentError(f"HTTP {exc.status}: {_content(exc)}") from exc
            except SalesforceError as exc:  # malformed request, not found, ...
                raise PermanentError(f"{type(exc).__name__}: {_content(exc)}") from exc

        return self._retry(attempt)

    # --------------------------------------------------------- protocol
    def fetch_closed_won(self, since: datetime | None) -> Iterator[dict[str, Any]]:
        # ">=" not ">": records sharing the watermark's second are re-read and
        # skipped by the idempotency hash instead of being silently lost.
        clause = f" AND LastModifiedDate >= {soql_datetime(since)}" if since else ""
        soql = CLOSED_WON_SOQL.format(since_clause=clause)
        result = self._call(lambda: self._sf.query_all(soql))
        log.info("Salesforce returned %d won opportunities", result["totalSize"])
        yield from result["records"]

    def update_order_status(self, status: ErpOrderStatus) -> None:
        fields = {
            "ERP_Order_Ref__c": status.erp_order_ref,
            "ERP_Invoice_Status__c": status.invoice_status.value,
            "ERP_Amount_Due__c": float(status.amount_due),
            "ERP_Last_Sync__c": soql_datetime(status.updated_at),
        }
        # sObject attributes are resolved dynamically by simple-salesforce.
        self._call(lambda: self._sf.Opportunity.update(status.external_id, fields))  # type: ignore[operator]
