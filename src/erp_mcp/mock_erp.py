"""A small in-memory ERP with a REST API, so the server can be run and tested without a real system.

It behaves like the systems this pattern is meant for: an API key, paging,
purchase orders with a remaining amount, duplicate detection and idempotent posting.
"""

from __future__ import annotations

import os
from decimal import Decimal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

VENDORS = [
    {
        "id": "V1001",
        "name": "Kuzey Ambalaj Sanayi A.Ş.",
        "tax_id": "4821963570",
        "currency": "TRY",
        "blocked": False,
    },
    {
        "id": "V1002",
        "name": "Marmara Kimya Ltd. Şti.",
        "tax_id": "7310458826",
        "currency": "TRY",
        "blocked": False,
    },
    {
        "id": "V1003",
        "name": "Ege Lojistik A.Ş.",
        "tax_id": "2957604418",
        "currency": "TRY",
        "blocked": True,
    },
    {
        "id": "V1004",
        "name": "Northwind Supplies Ltd.",
        "tax_id": "9038271645",
        "currency": "EUR",
        "blocked": False,
    },
    {
        "id": "V1005",
        "name": "Bluefield Components GmbH",
        "tax_id": "6149380252",
        "currency": "EUR",
        "blocked": False,
    },
]
PURCHASE_ORDERS = [
    {
        "number": "4500001201",
        "vendor_id": "V1001",
        "currency": "TRY",
        "amount": "120000.00",
        "invoiced": "45000.00",
        "status": "open",
    },
    {
        "number": "4500001202",
        "vendor_id": "V1001",
        "currency": "TRY",
        "amount": "18000.00",
        "invoiced": "18000.00",
        "status": "closed",
    },
    {
        "number": "4500001203",
        "vendor_id": "V1002",
        "currency": "TRY",
        "amount": "64500.00",
        "invoiced": "0.00",
        "status": "open",
    },
    {
        "number": "4500001204",
        "vendor_id": "V1004",
        "currency": "EUR",
        "amount": "9800.00",
        "invoiced": "2450.00",
        "status": "open",
    },
    {
        "number": "4500001205",
        "vendor_id": "V1004",
        "currency": "EUR",
        "amount": "3100.00",
        "invoiced": "0.00",
        "status": "open",
    },
    {
        "number": "4500001206",
        "vendor_id": "V1005",
        "currency": "EUR",
        "amount": "27500.00",
        "invoiced": "27500.00",
        "status": "closed",
    },
]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(api_key: str = "dev-key") -> Starlette:
    vendors = [dict(v) for v in VENDORS]
    orders = [dict(o) for o in PURCHASE_ORDERS]
    invoices: list[dict] = []
    by_idempotency_key: dict[str, dict] = {}

    def guard(request: Request) -> JSONResponse | None:
        if request.headers.get("x-api-key") != api_key:
            return _error(401, "unauthorized", "missing or wrong API key")
        return None

    def with_remaining(order: dict) -> dict:
        return {**order, "remaining": str(Decimal(order["amount"]) - Decimal(order["invoiced"]))}

    async def list_vendors(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        query = request.query_params.get("query", "").casefold()
        hits = [v for v in vendors if query in v["name"].casefold() or query in v["tax_id"]]
        return JSONResponse({"items": hits, "total": len(hits)})

    async def list_orders(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        q = request.query_params
        hits = [o for o in orders if o["vendor_id"] == q.get("vendor_id", o["vendor_id"])]
        if q.get("status"):
            hits = [o for o in hits if o["status"] == q["status"]]
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 20))
        page = [with_remaining(o) for o in hits[offset : offset + limit]]
        return JSONResponse({"items": page, "total": len(hits)})

    async def get_order(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        number = request.path_params["number"]
        order = next((o for o in orders if o["number"] == number), None)
        return (
            JSONResponse(with_remaining(order))
            if order
            else _error(404, "not_found", f"no purchase order {number}")
        )

    async def list_invoices(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        q = request.query_params
        hits = [
            i
            for i in invoices
            if i["vendor_id"] == q.get("vendor_id") and i["reference"] == q.get("reference")
        ]
        return JSONResponse({"items": hits, "total": len(hits)})

    async def post_invoice(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        key = request.headers.get("idempotency-key")
        if key and key in by_idempotency_key:
            return JSONResponse(by_idempotency_key[key], status_code=200)
        body = await request.json()
        vendor = next((v for v in vendors if v["id"] == body["vendor_id"]), None)
        if vendor is None:
            return _error(404, "not_found", f"no vendor {body['vendor_id']}")
        if vendor["blocked"]:
            return _error(409, "vendor_blocked", f"vendor {vendor['id']} is blocked for posting")
        if any(
            i["vendor_id"] == vendor["id"] and i["reference"] == body["reference"] for i in invoices
        ):
            return _error(
                409,
                "duplicate",
                f"reference {body['reference']} is already posted for {vendor['id']}",
            )
        order = None
        if body.get("purchase_order"):
            order = next((o for o in orders if o["number"] == body["purchase_order"]), None)
            if order is None or order["vendor_id"] != vendor["id"]:
                return _error(
                    422, "invalid_purchase_order", "purchase order does not belong to this vendor"
                )
            net = Decimal(body["gross_amount"]) - Decimal(body["tax_amount"])
            if net > Decimal(order["amount"]) - Decimal(order["invoiced"]):
                return _error(
                    422, "exceeds_purchase_order", "net amount is above the remaining order amount"
                )
            order["invoiced"] = str(Decimal(order["invoiced"]) + net)
            if Decimal(order["invoiced"]) == Decimal(order["amount"]):
                order["status"] = "closed"
        document = {**body, "document_number": f"51{len(invoices) + 1:08d}", "status": "posted"}
        invoices.append(document)
        if key:
            by_idempotency_key[key] = document
        return JSONResponse(document, status_code=201)

    return Starlette(
        routes=[
            Route("/vendors", list_vendors),
            Route("/purchase-orders", list_orders),
            Route("/purchase-orders/{number}", get_order),
            Route("/invoices", list_invoices),
            Route("/invoices", post_invoice, methods=["POST"]),
        ]
    )


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(os.environ.get("ERP_API_KEY", "dev-key")), host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
