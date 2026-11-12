"""The MCP server: four tools over the ERP client."""

from __future__ import annotations

import hashlib
import os
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from erp_mcp.client import ErpClient, ErpError

INSTRUCTIONS = (
    "Tools for looking up vendors and purchase orders and for posting vendor invoices. "
    "Look the vendor up before posting. post_vendor_invoice only posts when confirm is true; "
    "call it without confirm first, show the preview to the user and post after they agree."
)
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# What the model should do next, per ERP error code.
ADVICE = {
    "not_found": "Check the identifier with a search tool before retrying.",
    "duplicate": "This invoice is already in the ERP. Do not post it again.",
    "vendor_blocked": "Tell the user; posting to a blocked vendor needs someone in accounting.",
    "exceeds_purchase_order": "Ask the user whether another purchase order applies.",
    "invalid_purchase_order": "List the vendor's open purchase orders and pick one of them.",
    "unauthorized": "The server is misconfigured. Tell the user; retrying will not help.",
    "timeout": "Nothing is known about the outcome. For a posting, retry the same call: it is idempotent.",
    "unreachable": "The ERP is down or unreachable. Tell the user.",
}


class Vendor(BaseModel):
    id: str
    name: str
    tax_id: str
    currency: str
    blocked: bool


class PurchaseOrder(BaseModel):
    number: str
    vendor_id: str
    currency: str
    amount: Decimal
    invoiced: Decimal
    remaining: Decimal
    status: str


class PurchaseOrderPage(BaseModel):
    items: list[PurchaseOrder]
    total: int
    next_offset: int | None = Field(
        description="Pass as offset to get the next page; null on the last page"
    )


class Check(BaseModel):
    name: str
    passed: bool
    detail: str


class PostingResult(BaseModel):
    posted: bool
    document_number: str | None = None
    checks: list[Check]
    next_step: str


def _fail(error: ErpError) -> ToolError:
    advice = ADVICE.get(error.code, "")
    return ToolError(f"{error.code}: {error.message}. {advice}".strip())


def build_server(erp: ErpClient) -> MCPServer:
    server = MCPServer("erp", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ)
    async def search_vendors(
        query: Annotated[
            str, Field(min_length=2, description="Part of the vendor name, or a tax number")
        ],
    ) -> list[Vendor]:
        """Find vendors by name or tax number. Returns at most 10 matches."""
        try:
            return [Vendor(**v) for v in (await erp.search_vendors(query))[:10]]
        except ErpError as error:
            raise _fail(error) from error

    @server.tool(annotations=READ)
    async def list_purchase_orders(
        vendor_id: Annotated[str, Field(description="Vendor id from search_vendors, e.g. V1001")],
        status: Literal["open", "closed", "all"] = "open",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> PurchaseOrderPage:
        """List a vendor's purchase orders with the amount still open on each."""
        try:
            page = await erp.list_purchase_orders(
                vendor_id, None if status == "all" else status, limit, offset
            )
        except ErpError as error:
            raise _fail(error) from error
        end = offset + len(page["items"])
        return PurchaseOrderPage(
            items=page["items"],
            total=page["total"],
            next_offset=end if end < page["total"] else None,
        )

    @server.tool(annotations=READ)
    async def get_purchase_order(number: str) -> PurchaseOrder:
        """Get one purchase order by its number."""
        try:
            return PurchaseOrder(**await erp.get_purchase_order(number))
        except ErpError as error:
            raise _fail(error) from error

    @server.tool(annotations=WRITE)
    async def post_vendor_invoice(
        vendor_tax_id: Annotated[str, Field(description="Tax number printed on the invoice")],
        reference: Annotated[str, Field(description="Invoice number as printed")],
        document_date: date,
        gross_amount: Annotated[Decimal, Field(gt=0)],
        tax_amount: Annotated[Decimal, Field(ge=0)],
        currency: Annotated[str, Field(pattern="^[A-Z]{3}$")],
        purchase_order: str | None = None,
        confirm: bool = False,
    ) -> PostingResult:
        """Post a vendor invoice. Without confirm it only runs the checks and returns a preview.

        Posting is idempotent: repeating a confirmed call with the same vendor and
        reference returns the document that was already created.
        """
        try:
            checks, vendor = await _checks(
                erp, vendor_tax_id, reference, gross_amount, tax_amount, currency, purchase_order
            )
            if not all(c.passed for c in checks):
                return PostingResult(
                    posted=False,
                    checks=checks,
                    next_step="Resolve the failed checks; nothing was posted.",
                )
            if not confirm:
                return PostingResult(
                    posted=False,
                    checks=checks,
                    next_step="All checks passed. Call again with confirm=true to post.",
                )
            payload = {
                "vendor_id": vendor.id,
                "reference": reference,
                "document_date": document_date.isoformat(),
                "gross_amount": str(gross_amount),
                "tax_amount": str(tax_amount),
                "currency": currency,
                "purchase_order": purchase_order,
            }
            key = hashlib.sha256(f"{vendor.id}|{reference}".encode()).hexdigest()
            document = await erp.post_invoice(payload, key)
        except ErpError as error:
            raise _fail(error) from error
        return PostingResult(
            posted=True,
            document_number=document["document_number"],
            checks=checks,
            next_step="Posted.",
        )

    return server


async def _checks(
    erp: ErpClient,
    tax_id: str,
    reference: str,
    gross: Decimal,
    tax: Decimal,
    currency: str,
    order_number: str | None,
) -> tuple[list[Check], Vendor | None]:
    matches = [Vendor(**v) for v in await erp.search_vendors(tax_id) if v["tax_id"] == tax_id]
    if len(matches) != 1:
        return [
            Check(
                name="vendor",
                passed=False,
                detail=f"{len(matches)} vendors have tax number {tax_id}",
            )
        ], None
    vendor = matches[0]
    checks = [
        Check(
            name="vendor",
            passed=not vendor.blocked,
            detail=f"{vendor.id} {vendor.name}" + (" is blocked" if vendor.blocked else ""),
        ),
        Check(name="amounts", passed=tax < gross, detail=f"gross {gross}, tax {tax}"),
        Check(
            name="currency",
            passed=currency == vendor.currency,
            detail=f"invoice {currency}, vendor {vendor.currency}",
        ),
    ]
    duplicates = await erp.find_invoices(vendor.id, reference)
    checks.append(
        Check(
            name="duplicate",
            passed=not duplicates,
            detail=f"{len(duplicates)} posted with reference {reference}",
        )
    )
    if order_number:
        try:
            order = PurchaseOrder(**await erp.get_purchase_order(order_number))
        except ErpError as error:
            if error.code != "not_found":
                raise
            checks.append(
                Check(name="purchase_order", passed=False, detail=f"{order_number} does not exist")
            )
        else:
            ok = (
                order.vendor_id == vendor.id
                and order.status == "open"
                and gross - tax <= order.remaining
            )
            checks.append(
                Check(
                    name="purchase_order",
                    passed=ok,
                    detail=f"{order.status}, remaining {order.remaining}, net {gross - tax}",
                )
            )
    return checks, vendor


def main() -> None:
    erp = ErpClient.from_settings(
        os.environ.get("ERP_BASE_URL", "http://127.0.0.1:8080"), os.environ["ERP_API_KEY"]
    )
    build_server(erp).run("stdio")


if __name__ == "__main__":
    main()
