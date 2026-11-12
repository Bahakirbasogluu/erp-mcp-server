"""HTTP client for the ERP. The only module that knows its paths, headers and error format."""

from __future__ import annotations

from typing import Any

import httpx


class ErpError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ErpClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    @classmethod
    def from_settings(cls, base_url: str, api_key: str, timeout: float = 15.0) -> ErpClient:
        return cls(
            httpx.AsyncClient(base_url=base_url, headers={"x-api-key": api_key}, timeout=timeout)
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ErpError("timeout", "the ERP did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ErpError("unreachable", "the ERP could not be reached") from exc
        if response.is_success:
            return response.json()
        try:
            error = response.json()["error"]
            raise ErpError(error["code"], error["message"], response.status_code)
        except (ValueError, KeyError, TypeError):
            raise ErpError(
                "http_error",
                f"the ERP answered with status {response.status_code}",
                response.status_code,
            ) from None

    async def search_vendors(self, query: str) -> list[dict[str, Any]]:
        return (await self._request("GET", "/vendors", params={"query": query}))["items"]

    async def list_purchase_orders(
        self, vendor_id: str, status: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        params = {"vendor_id": vendor_id, "limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return await self._request("GET", "/purchase-orders", params=params)

    async def get_purchase_order(self, number: str) -> dict[str, Any]:
        return await self._request("GET", f"/purchase-orders/{number}")

    async def find_invoices(self, vendor_id: str, reference: str) -> list[dict[str, Any]]:
        return (
            await self._request(
                "GET", "/invoices", params={"vendor_id": vendor_id, "reference": reference}
            )
        )["items"]

    async def post_invoice(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/invoices", json=payload, headers={"idempotency-key": idempotency_key}
        )
