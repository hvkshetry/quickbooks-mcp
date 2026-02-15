# quickbooks-mcp

MCP server for QuickBooks Online — 6 parameterized tools covering ~120 operations across accounts, customers, vendors, employees, transactions (13 types), items, reference data, and 11 financial reports.

Built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk) and [python-quickbooks](https://github.com/ej2/python-quickbooks). Designed for use with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and other MCP-compatible clients.

## Tools

| Tool | Operations | Covers |
|------|-----------|--------|
| `account` | list, get, create, update, deactivate, search | Chart of accounts management |
| `party` | list, get, create, update, deactivate, search | Customers, vendors, employees (`party_type` param) |
| `transaction` | list, get, create, update, delete, void, search | 13 types: invoice, bill, bill_payment, payment, deposit, transfer, journal_entry, purchase, estimate, credit_memo, sales_receipt, refund_receipt, vendor_credit |
| `item` | list, get, create, update, deactivate, search | Products and services |
| `reference` | list_tax_codes, list_classes, list_departments, list_terms, list_payment_methods, get_company_info, get_preferences | Lookup data and company settings |
| `report` | profit_and_loss, balance_sheet, trial_balance, cash_flow, general_ledger, ar_aging_summary, ar_aging_detail, ap_aging_summary, ap_aging_detail, customer_balance, vendor_balance | Financial reports with date range and filter params |

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- An Intuit Developer account with a QuickBooks app ([create one here](https://developer.intuit.com))

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/hvkshetry/quickbooks-mcp.git
cd quickbooks-mcp
cp .env.example .env
```

Edit `.env` with your Intuit app credentials:

```
QBO_CLIENT_ID=<from Intuit Developer Portal>
QBO_CLIENT_SECRET=<from Intuit Developer Portal>
QBO_ENVIRONMENT=production
QBO_REDIRECT_URI=http://localhost:8010/callback
```

### 2. Register the redirect URI

In the [Intuit Developer Portal](https://developer.intuit.com):
1. Open your app → **Keys & credentials**
2. Under **Redirect URIs**, add: `http://localhost:8010/callback`

> **Production note:** Intuit requires HTTPS redirect URIs for production keys. For local development, use your Development keys (which allow HTTP localhost). Alternatively, use a tunnel like [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) to expose localhost over HTTPS, register the tunnel URL as your redirect URI, and set `QBO_REDIRECT_URI` accordingly.

### 3. Authorize

```bash
uv run python auth_flow.py
```

This opens your browser to Intuit's OAuth page. Sign in, select your QuickBooks company, and authorize. The script writes `QBO_REFRESH_TOKEN` and `QBO_REALM_ID` to `.env` automatically.

### 4. Run the server

```bash
uv run python server.py        # STDIO transport (default)
uv run python server.py sse    # SSE transport on port 8020
```

## MCP Client Configuration

### Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "quickbooks-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/quickbooks-mcp", "run", "python", "server.py"]
    }
  }
}
```

### Other MCP clients

Any MCP-compatible client can connect via STDIO or SSE transport. See the [MCP specification](https://modelcontextprotocol.io/) for details.

## Usage Examples

### List accounts

```
account(operation="list", limit=10, account_type="Expense")
```

### Search for a vendor

```
party(operation="search", party_type="vendor", query="Acme")
```

### Create an invoice

```
transaction(operation="create", entity_type="invoice", data={
  "CustomerRef": {"value": "42"},
  "TxnDate": "2025-01-15",
  "DueDate": "2025-02-14",
  "Line": [{
    "Amount": 500.00,
    "DetailType": "SalesItemLineDetail",
    "SalesItemLineDetail": {
      "ItemRef": {"value": "1"},
      "Qty": 10,
      "UnitPrice": 50.00
    }
  }]
})
```

### Create a journal entry

```
transaction(operation="create", entity_type="journal_entry", data={
  "TxnDate": "2025-01-31",
  "PrivateNote": "Accrue January utilities",
  "Line": [
    {
      "Amount": 2500.00,
      "DetailType": "JournalEntryLineDetail",
      "JournalEntryLineDetail": {
        "PostingType": "Debit",
        "AccountRef": {"value": "62"}
      }
    },
    {
      "Amount": 2500.00,
      "DetailType": "JournalEntryLineDetail",
      "JournalEntryLineDetail": {
        "PostingType": "Credit",
        "AccountRef": {"value": "45"}
      }
    }
  ]
})
```

### Run a P&L report

```
report(operation="profit_and_loss", start_date="2025-01-01", end_date="2025-12-31")
```

### Get AR aging

```
report(operation="ar_aging_summary", end_date="2025-12-31")
```

## Key Patterns

**Search before create** — Always search for existing entities before creating to avoid duplicates:
```
account(operation="search", query="Utilities")
# If not found, then create
```

**Get before update** — QBO requires the current `SyncToken` for updates. The client handles this automatically by fetching the entity before applying changes:
```
transaction(operation="get", entity_type="bill", entity_id=123)
transaction(operation="update", entity_type="bill", entity_id=123, data={...})
```

**Token refresh** — The server automatically refreshes expired OAuth tokens and persists the new refresh token to `.env`. If authentication fails persistently, re-run `uv run python auth_flow.py`.

## Architecture

```
quickbooks-mcp/
├── server.py       # FastMCP server — 6 tools with @mcp.tool decorators
├── client.py       # QBOClient wrapper — async safety, token refresh, rate limiting
├── auth_flow.py    # One-time OAuth flow (port 8010)
├── pyproject.toml  # Dependencies: python-quickbooks, intuit-oauth, mcp, dotenv
├── .env.example    # Environment variable template
└── .env            # Your credentials (git-ignored)
```

- All `python-quickbooks` calls are wrapped in `asyncio.to_thread()` for async safety
- Token refresh is locked (`asyncio.Lock`) to prevent concurrent refresh races
- Rate limiting: automatic exponential backoff on HTTP 429 (QBO limits: 500 req/min, 10 req/sec)
- Token persistence: atomic write to `.env` via temp file + `os.replace()`

## License

MIT
