# erp-mcp

[![CI](https://github.com/Bahakirbasogluu/erp-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Bahakirbasogluu/erp-mcp-server/actions/workflows/ci.yml)

A Model Context Protocol server that gives an agent guarded access to an ERP REST API: look up vendors and purchase orders, and post vendor invoices. It ships with a small mock ERP, so it runs and is tested without a real system.

The interesting part of putting an agent in front of an accounting system is not the read tools. It is making sure a write happens once, after a person has seen it, and that a failure tells the model what to do next. This repository is a worked example of that. The code is written from scratch; it contains no client code or data.

## Tools

| Tool | Kind | What it does |
| --- | --- | --- |
| `search_vendors` | read | Find vendors by name or tax number, at most 10 matches |
| `list_purchase_orders` | read | A vendor's orders with the amount still open, paged |
| `get_purchase_order` | read | One order by number |
| `post_vendor_invoice` | write | Run the posting checks; post only when `confirm` is true |

## Design decisions

- **Writes are two-step.** `post_vendor_invoice` without `confirm` runs every check and returns a preview; nothing is sent to the ERP. The server instructions tell the model to show the preview and post only after the user agrees. A failed check blocks the posting even when `confirm` is true.
- **Posting is idempotent.** The idempotency key is derived from vendor and invoice reference, so a retry after a timeout returns the document that was already created instead of creating a second one. A later attempt to post the same reference is caught by the duplicate check before anything is written.
- **Checks are data, not prose.** The result lists each check (`vendor`, `amounts`, `currency`, `duplicate`, `purchase_order`) with pass or fail and the values compared, so the model can explain what is wrong and a host application can render it.
- **Errors say what to do next.** ERP errors are mapped to tool errors with a stable code and one line of advice per code: retry, ask the user, or stop. A timeout on a posting says the call is safe to repeat.
- **Arguments are validated by the schema.** Amount ranges, the currency pattern, the status enum and page limits are part of the tool's input schema, so invalid calls are rejected before the ERP is called.
- **The ERP is behind one module.** `client.py` is the only place that knows paths, headers and the error format. The API key lives there and never appears in tool input or output.
- **Typed, bounded results.** Tools return Pydantic models, so results carry an output schema. Lists are capped and paged with `next_offset`.

## Quick start

```bash
pip install -e ".[dev]"
pytest
```

Run it against the mock ERP:

```bash
ERP_API_KEY=dev-key erp-mock          # terminal 1: mock ERP on 127.0.0.1:8080
ERP_API_KEY=dev-key erp-mcp           # terminal 2: MCP server on stdio
```

To use it from an MCP host, register the stdio command:

```json
{
  "mcpServers": {
    "erp": {
      "command": "erp-mcp",
      "env": { "ERP_BASE_URL": "http://127.0.0.1:8080", "ERP_API_KEY": "dev-key" }
    }
  }
}
```

A preview call and its result:

```json
{"vendor_tax_id": "4821963570", "reference": "FTR2026000123", "document_date": "2026-03-12",
 "gross_amount": "12000.00", "tax_amount": "2000.00", "currency": "TRY", "purchase_order": "4500001201"}
```

```json
{
  "posted": false,
  "document_number": null,
  "checks": [
    {"name": "vendor", "passed": true, "detail": "V1001 Kuzey Ambalaj Sanayi A.Ş."},
    {"name": "amounts", "passed": true, "detail": "gross 12000.00, tax 2000.00"},
    {"name": "currency", "passed": true, "detail": "invoice TRY, vendor TRY"},
    {"name": "duplicate", "passed": true, "detail": "0 posted with reference FTR2026000123"},
    {"name": "purchase_order", "passed": true, "detail": "open, remaining 75000.00, net 10000.00"}
  ],
  "next_step": "All checks passed. Call again with confirm=true to post."
}
```

## Tests

The suite connects an MCP client to the server in memory and the server to the mock ERP through an ASGI transport, so every test goes through the protocol and the HTTP layer without opening a socket. It covers:

- tool listing and read/write annotations
- paging
- a preview writes nothing
- a confirmed posting writes exactly once, also when the call is repeated
- the same idempotency key returns the same document
- each failed check blocks posting even with `confirm`
- invalid arguments never reach the ERP
- the API key does not appear in tool output
- a wrong API key is reported as a configuration problem

## Layout

```
src/erp_mcp/
  server.py      tools, checks, error advice
  client.py      HTTP client and error mapping
  mock_erp.py    in-memory ERP with a REST API
tests/
  test_server.py
```

## Pointing it at a real system

Replace `client.py`. The tools depend on five client methods (`search_vendors`, `list_purchase_orders`, `get_purchase_order`, `find_invoices`, `post_invoice`) and on `ErpError` codes; the mapping in `ADVICE` lists the codes the server knows how to explain. The mock ERP is not a model of any particular product.

Requires the MCP Python SDK 2.x.

## License

MIT
