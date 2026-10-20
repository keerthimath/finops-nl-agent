"""Thin, read-only ADX client. Azure SDK imports are lazy so tests run without them."""
from __future__ import annotations

import datetime as dt
import decimal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings


class AdxError(RuntimeError):
    """Query failed in ADX. The message is safe to show to the model."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int            # rows returned by ADX (after server-side cap)
    truncated: bool           # True if ADX hit the row cap
    elapsed_ms: int = 0
    request_id: str = ""
    extra: dict = field(default_factory=dict)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


class AdxClient:
    """token_provider=None -> use the developer's `az login` identity (AUTH_MODE=dev).
    token_provider=callable -> returns an ADX access token for the signed-in user (AUTH_MODE=obo).
    """

    def __init__(self, settings: Settings, token_provider: Callable[[], str] | None = None):
        self._s = settings
        self._token_provider = token_provider
        self._client = None

    def _get_client(self):
        if self._client is None:
            from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

            uri = self._s.adx_cluster_uri
            if self._token_provider is not None:
                kcsb = KustoConnectionStringBuilder.with_token_provider(uri, self._token_provider)
            else:
                kcsb = KustoConnectionStringBuilder.with_az_cli_authentication(uri)
            self._client = KustoClient(kcsb)
        return self._client

    def query(self, kql: str, *, request_id: str | None = None) -> QueryResult:
        """Blocking call; run it with asyncio.to_thread from async code."""
        from azure.kusto.data import ClientRequestProperties

        rid = request_id or str(uuid.uuid4())
        props = ClientRequestProperties()
        props.client_request_id = f"finops-nl;{rid}"
        props.application = "finops-nl-agent"
        props.set_option("request_readonly", True)
        # timedelta, not a string: the SDK adds it to its client-side delta, then sends it as "H:MM:SS".
        props.set_option("servertimeout", dt.timedelta(seconds=self._s.query_timeout_s))
        props.set_option("truncationmaxrecords", self._s.max_rows)
        props.set_option("notruncation", False)

        t0 = time.monotonic()
        try:
            response = self._get_client().execute(self._s.adx_database, kql, props)
        except Exception as exc:  # noqa: BLE001 - surface ADX message to the model
            raise AdxError(_clean_error(exc)) from exc

        table = response.primary_results[0] if response.primary_results else None
        if table is None:
            return QueryResult([], [], 0, False, int((time.monotonic() - t0) * 1000), rid)
        columns = [c.column_name for c in table.columns]
        rows = []
        for r in table:
            d = r.to_dict()
            rows.append([_jsonable(d.get(c)) for c in columns])
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=len(rows) >= self._s.max_rows,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            request_id=rid,
        )


def _clean_error(exc: Exception) -> str:
    msg = str(exc).strip().replace("\r", "")
    return msg[:800] or exc.__class__.__name__
