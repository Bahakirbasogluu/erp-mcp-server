import json

import httpx
import pytest
from mcp import Client

from erp_mcp.client import ErpClient
from erp_mcp.mock_erp import create_app
from erp_mcp.server import build_server

pytestmark = pytest.mark.anyio

INVOICE = {
    "vendor_tax_id": "4821963570",
    "reference": "FTR2026000123",
    "document_date": "2026-03-12",
    "gross_amount": "12000.00",
    "tax_amount": "2000.00",
    "currency": "TRY",
    "purchase_order": "4500001201",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def requests():
    return []


@pytest.fixture
async def client(requests):
    async def record(request):
        requests.append(request)

    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app("test-key")),
        base_url="http://erp",
        headers={"x-api-key": "test-key"},
        event_hooks={"request": [record]},
    )
    async with Client(build_server(ErpClient(http))) as mcp_client:
        yield mcp_client
    await http.aclose()


def writes(requests):
    return [r for r in requests if r.method == "POST"]


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    return result, result.structured_content


async def test_tools_are_listed_with_read_and_write_hints(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "search_vendors",
        "list_purchase_orders",
        "get_purchase_order",
        "post_vendor_invoice",
    }
    assert tools["search_vendors"].annotations.read_only_hint is True
    assert tools["post_vendor_invoice"].annotations.read_only_hint is False
    assert "confirm" in tools["post_vendor_invoice"].input_schema["properties"]


async def test_search_vendors_by_name(client):
    _, data = await call(client, "search_vendors", query="kuzey")
    assert [v["id"] for v in data["result"]] == ["V1001"]


async def test_purchase_orders_are_paged(client):
    _, first = await call(client, "list_purchase_orders", vendor_id="V1004", limit=1)
    assert (first["total"], first["next_offset"], len(first["items"])) == (2, 1, 1)
    _, second = await call(client, "list_purchase_orders", vendor_id="V1004", limit=1, offset=1)
    assert second["next_offset"] is None
    assert first["items"][0]["number"] != second["items"][0]["number"]


async def test_unknown_purchase_order_is_a_tool_error_with_advice(client):
    result, _ = await call(client, "get_purchase_order", number="4599999999")
    assert result.is_error
    assert "not_found" in result.content[0].text and "search tool" in result.content[0].text


async def test_posting_without_confirm_previews_and_writes_nothing(client, requests):
    result, data = await call(client, "post_vendor_invoice", **INVOICE)
    assert not result.is_error
    assert data["posted"] is False and data["document_number"] is None
    assert all(c["passed"] for c in data["checks"])
    assert writes(requests) == []


async def test_confirmed_posting_creates_one_document_even_when_repeated(client, requests):
    _, first = await call(client, "post_vendor_invoice", **INVOICE, confirm=True)
    assert first["posted"] is True and first["document_number"]
    _, order = await call(client, "get_purchase_order", number="4500001201")
    assert order["remaining"] == "65000.00"
    _, again = await call(client, "post_vendor_invoice", **INVOICE, confirm=True)
    assert again["posted"] is False
    assert {c["name"]: c["passed"] for c in again["checks"]}["duplicate"] is False
    assert len(writes(requests)) == 1


async def test_retry_with_the_same_idempotency_key_returns_the_same_document(requests):
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app("k")),
        base_url="http://erp",
        headers={"x-api-key": "k"},
    )
    erp = ErpClient(http)
    payload = {
        "vendor_id": "V1002",
        "reference": "A-1",
        "document_date": "2026-03-12",
        "gross_amount": "120",
        "tax_amount": "20",
        "currency": "TRY",
    }
    first = await erp.post_invoice(payload, "same-key")
    second = await erp.post_invoice(payload, "same-key")
    assert first["document_number"] == second["document_number"]
    await http.aclose()


@pytest.mark.parametrize(
    ("change", "failed_check"),
    [
        ({"vendor_tax_id": "2957604418", "purchase_order": None}, "vendor"),
        ({"currency": "EUR"}, "currency"),
        ({"gross_amount": "200000.00"}, "purchase_order"),
        ({"purchase_order": "4500001202"}, "purchase_order"),
        ({"purchase_order": "4599999999"}, "purchase_order"),
        ({"vendor_tax_id": "0000000000"}, "vendor"),
    ],
)
async def test_failed_checks_block_posting_even_with_confirm(
    client, requests, change, failed_check
):
    args = {k: v for k, v in {**INVOICE, **change}.items() if v is not None}
    _, data = await call(client, "post_vendor_invoice", **args, confirm=True)
    assert data["posted"] is False
    assert failed_check in [c["name"] for c in data["checks"] if not c["passed"]]
    assert writes(requests) == []


async def test_invalid_arguments_are_rejected_before_the_erp_is_called(client, requests):
    result, _ = await call(client, "post_vendor_invoice", **{**INVOICE, "currency": "tl"})
    assert result.is_error
    assert requests == []


async def test_api_key_never_appears_in_tool_output(client):
    result, _ = await call(client, "search_vendors", query="ltd")
    assert "test-key" not in json.dumps(result.model_dump(mode="json"))


async def test_wrong_api_key_is_reported_as_configuration_problem():
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app("right")),
        base_url="http://erp",
        headers={"x-api-key": "wrong"},
    )
    async with Client(build_server(ErpClient(http))) as mcp_client:
        result = await mcp_client.call_tool("search_vendors", {"query": "kuzey"})
    assert result.is_error and "misconfigured" in result.content[0].text
    await http.aclose()
