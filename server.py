#!/usr/bin/env python3
"""QuickBooks Online MCP Server — 6 parameterized tools, ~120 operations.

Dual transport: STDIO (default) or SSE (pass 'sse' argument).
Uses python-quickbooks library wrapped in asyncio.to_thread() for async safety.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("quickbooks_mcp")

# ── Lazy client singleton ────────────────────────────────────────────

_client = None
_client_lock = asyncio.Lock()


async def get_client():
    """Get or create the QBO client singleton."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        from client import QBOClient

        _client = QBOClient()
        logger.info("QBO client initialized")
    return _client


def _json(data: Any) -> str:
    """Serialize response data to JSON string."""
    return json.dumps(data, indent=2, default=str)


def _error(e: Exception, context: str = "") -> str:
    """Format an actionable error message for the agent."""
    msg = str(e)
    if "401" in msg or "authorization" in msg.lower() or "token" in msg.lower():
        hint = "Authentication failed. The token may have expired. Try restarting the server or re-running auth_flow.py."
    elif "404" in msg or "not found" in msg.lower():
        hint = "Entity not found. Use the list/search operation first to find valid IDs."
    elif "403" in msg or "permission" in msg.lower() or "forbidden" in msg.lower():
        hint = "Permission denied. Check QBO user permissions for this operation."
    elif "400" in msg or "bad request" in msg.lower() or "validation" in msg.lower():
        hint = "Invalid request data. Check required fields and data format. For updates, ensure SyncToken is current (use get first)."
    elif "429" in msg or "throttl" in msg.lower() or "rate" in msg.lower():
        hint = "Rate limited by QBO API. Wait a moment and retry."
    elif "QBO_REFRESH_TOKEN" in msg or "QBO_REALM_ID" in msg:
        hint = "Run `uv run python auth_flow.py` to complete OAuth setup."
    else:
        hint = "Check the operation name, entity_type, and parameters."
    prefix = f"[{context}] " if context else ""
    logger.error(f"{prefix}{msg}")
    return _json({"error": f"{prefix}{msg}", "hint": hint})


def _safe(tool_name: str):
    """Decorator to add error handling to tool functions."""
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                op = kwargs.get("operation", args[0] if args else "unknown")
                return _error(e, f"{tool_name}.{op}")
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════
# Tool Definitions — 6 parameterized tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    annotations={
        "title": "QuickBooks Chart of Accounts",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("account")
async def account(
    operation: str,
    account_id: int = None,
    data: dict = None,
    query: str = None,
    account_type: str = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Chart of Accounts management in QuickBooks Online.

    Operations:
      Read: list, get, search
      Write: create, update, deactivate

    Args:
        operation: One of the operations listed above.
        account_id: Account ID (required for get/update/deactivate).
        data: Dict of fields for create/update. Key fields:
            - Name (str, required for create)
            - AccountType (str: Bank, Other Current Asset, Fixed Asset, Other Asset,
              Accounts Receivable, Equity, Expense, Other Expense, Cost of Goods Sold,
              Accounts Payable, Credit Card, Long Term Liability, Other Current Liability, Income, Other Income)
            - AccountSubType (str, see QBO docs for valid sub-types per AccountType)
            - Description (str)
            - AcctNum (str, account number)
        query: Search text for search operation (matches Name).
        account_type: Filter by AccountType for list operation.
        limit: Max results (default 100).
        offset: Pagination offset.

    Returns:
        JSON string with account data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        return _json(await c.account_list(limit, offset, account_type))

    elif operation == "get":
        return _json(await c.account_get(account_id))

    elif operation == "create":
        return _json(await c.account_create(data or {}))

    elif operation == "update":
        return _json(await c.account_update(account_id, data or {}))

    elif operation == "deactivate":
        return _json(await c.account_deactivate(account_id))

    elif operation == "search":
        return _json(await c.account_search(query or "", limit))

    else:
        return _json({"error": f"Unknown operation: {operation}. Valid: list, get, create, update, deactivate, search"})


@mcp.tool(
    annotations={
        "title": "QuickBooks Customers, Vendors & Employees",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("party")
async def party(
    operation: str,
    party_type: str = "customer",
    party_id: int = None,
    data: dict = None,
    query: str = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Customer, vendor, and employee management in QuickBooks Online.

    Operations:
      Read: list, get, search
      Write: create, update, deactivate

    Args:
        operation: One of the operations listed above.
        party_type: One of: customer, vendor, employee.
        party_id: Entity ID (required for get/update/deactivate).
        data: Dict of fields for create/update. Key fields:
            Customer: DisplayName, CompanyName, PrimaryEmailAddr, PrimaryPhone,
                BillAddr, ShipAddr, Balance (read-only)
            Vendor: DisplayName, CompanyName, PrimaryEmailAddr, PrimaryPhone,
                BillAddr, TaxIdentifier, Balance (read-only)
            Employee: DisplayName, GivenName, FamilyName, PrimaryEmailAddr,
                PrimaryPhone, SSN, HiredDate
        query: Search text for search operation (matches DisplayName).
        limit: Max results (default 100).
        offset: Pagination offset.

    Returns:
        JSON string with party data or {"error": "..."}.
    """
    c = await get_client()

    if party_type not in ("customer", "vendor", "employee"):
        return _json({"error": f"Invalid party_type: {party_type}. Must be customer, vendor, or employee."})

    if operation == "list":
        return _json(await c.party_list(party_type, limit, offset))

    elif operation == "get":
        return _json(await c.party_get(party_type, party_id))

    elif operation == "create":
        return _json(await c.party_create(party_type, data or {}))

    elif operation == "update":
        return _json(await c.party_update(party_type, party_id, data or {}))

    elif operation == "deactivate":
        return _json(await c.party_deactivate(party_type, party_id))

    elif operation == "search":
        return _json(await c.party_search(party_type, query or "", limit))

    else:
        return _json({"error": f"Unknown operation: {operation}. Valid: list, get, create, update, deactivate, search"})


@mcp.tool(
    annotations={
        "title": "QuickBooks Transactions",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("transaction")
async def transaction(
    operation: str,
    entity_type: str = "invoice",
    entity_id: int = None,
    data: dict = None,
    query: str = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """All transaction types in QuickBooks Online — the core bookkeeping workhorse.

    Operations:
      Read: list, get, search
      Write: create, update
      Delete: delete, void (not all entity types support void)

    Entity types: invoice, bill, bill_payment, payment, deposit, transfer,
      journal_entry, purchase, estimate, credit_memo, sales_receipt,
      refund_receipt, vendor_credit

    Args:
        operation: One of the operations listed above.
        entity_type: One of the entity types listed above.
        entity_id: Transaction ID (required for get/update/delete/void).
        data: Dict of fields for create/update. Structure varies by entity_type.
            Common patterns:
            - journal_entry: {Line: [{Amount, DetailType: "JournalEntryLineDetail",
                JournalEntryLineDetail: {PostingType: "Debit"|"Credit",
                AccountRef: {value: "id"}}}], TxnDate, PrivateNote}
            - invoice: {CustomerRef: {value: "id"}, Line: [{Amount, DetailType:
                "SalesItemLineDetail", SalesItemLineDetail: {ItemRef: {value: "id"}}}]}
            - bill: {VendorRef: {value: "id"}, Line: [{Amount, DetailType:
                "AccountBasedExpenseLineDetail", AccountBasedExpenseLineDetail:
                {AccountRef: {value: "id"}}}]}
        query: WHERE clause for search (e.g. "TxnDate >= '2024-01-01'").
        limit: Max results (default 100).
        offset: Pagination offset.

    Returns:
        JSON string with transaction data or {"error": "..."}.
    """
    c = await get_client()

    from client import TRANSACTION_TYPES, TRANSACTION_OPS

    if entity_type not in TRANSACTION_TYPES:
        valid = ", ".join(sorted(TRANSACTION_TYPES.keys()))
        return _json({"error": f"Invalid entity_type: {entity_type}. Valid: {valid}"})

    valid_ops = TRANSACTION_OPS.get(entity_type, set())
    if operation not in valid_ops:
        return _json({
            "error": f"Operation '{operation}' not supported for {entity_type}. Valid: {', '.join(sorted(valid_ops))}"
        })

    if operation == "list":
        return _json(await c.transaction_list(entity_type, limit, offset))

    elif operation == "get":
        return _json(await c.transaction_get(entity_type, entity_id))

    elif operation == "create":
        return _json(await c.transaction_create(entity_type, data or {}))

    elif operation == "update":
        return _json(await c.transaction_update(entity_type, entity_id, data or {}))

    elif operation == "delete":
        return _json(await c.transaction_delete(entity_type, entity_id))

    elif operation == "void":
        return _json(await c.transaction_void(entity_type, entity_id))

    elif operation == "search":
        return _json(await c.transaction_search(entity_type, query or "", limit))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "QuickBooks Products & Services",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("item")
async def item(
    operation: str,
    item_id: int = None,
    data: dict = None,
    query: str = None,
    item_type: str = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Products & services (line items) management in QuickBooks Online.

    Operations:
      Read: list, get, search
      Write: create, update, deactivate

    Args:
        operation: One of the operations listed above.
        item_id: Item ID (required for get/update/deactivate).
        data: Dict of fields for create/update. Key fields:
            - Name (str, required)
            - Type (str: Inventory, Service, NonInventory)
            - IncomeAccountRef (dict: {value: "account_id"})
            - ExpenseAccountRef (dict: {value: "account_id"})
            - AssetAccountRef (dict: {value: "account_id"}, Inventory only)
            - UnitPrice (decimal)
            - PurchaseCost (decimal)
            - QtyOnHand (decimal, Inventory only)
            - InvStartDate (str, Inventory only)
            - Description (str)
        query: Search text for search operation (matches Name).
        item_type: Filter by type for list: inventory, service, noninventory.
        limit: Max results (default 100).
        offset: Pagination offset.

    Returns:
        JSON string with item data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        return _json(await c.item_list(limit, offset, item_type))

    elif operation == "get":
        return _json(await c.item_get(item_id))

    elif operation == "create":
        return _json(await c.item_create(data or {}))

    elif operation == "update":
        return _json(await c.item_update(item_id, data or {}))

    elif operation == "deactivate":
        return _json(await c.item_deactivate(item_id))

    elif operation == "search":
        return _json(await c.item_search(query or "", limit))

    else:
        return _json({"error": f"Unknown operation: {operation}. Valid: list, get, create, update, deactivate, search"})


@mcp.tool(
    annotations={
        "title": "QuickBooks Reference Data & Settings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@_safe("reference")
async def reference(
    operation: str,
    entity_id: int = None,
) -> str:
    """Lookup data and company settings in QuickBooks Online. Mostly read-only.

    Operations:
      list_tax_codes, list_classes, list_departments, list_terms,
      list_payment_methods, get_company_info, get_preferences

    Args:
        operation: One of the operations listed above.
        entity_id: Optional entity ID (currently unused, reserved for future ops).

    Returns:
        JSON string with reference data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "get_company_info":
        return _json(await c.reference_get_company_info())

    elif operation == "get_preferences":
        return _json(await c.reference_get_preferences())

    elif operation in ("list_tax_codes", "list_classes", "list_departments",
                       "list_terms", "list_payment_methods"):
        return _json(await c.reference_list(operation))

    else:
        valid = "list_tax_codes, list_classes, list_departments, list_terms, list_payment_methods, get_company_info, get_preferences"
        return _json({"error": f"Unknown operation: {operation}. Valid: {valid}"})


@mcp.tool(
    annotations={
        "title": "QuickBooks Financial Reports",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@_safe("report")
async def report(
    operation: str,
    start_date: str = None,
    end_date: str = None,
    accounting_method: str = None,
    department: str = None,
    class_id: str = None,
    customer_id: str = None,
    vendor_id: str = None,
) -> str:
    """Financial reports from QuickBooks Online — essential for reconciliation and month-end close.

    Operations:
      profit_and_loss, balance_sheet, trial_balance, cash_flow,
      general_ledger, ar_aging_summary, ar_aging_detail,
      ap_aging_summary, ap_aging_detail, customer_balance, vendor_balance

    Args:
        operation: One of the report types listed above.
        start_date: Report start date (YYYY-MM-DD). Required for P&L, CF, GL.
        end_date: Report end date (YYYY-MM-DD). For aging reports, this sets the as-of date.
        accounting_method: "Cash" or "Accrual" (default depends on QBO company setting).
        department: Department ID to filter by.
        class_id: Class ID to filter by.
        customer_id: Customer ID to filter by (for customer-specific reports).
        vendor_id: Vendor ID to filter by (for vendor-specific reports).

    Returns:
        JSON string with structured report data including:
        - Header: report name, date range, accounting method
        - Columns: column definitions
        - Rows: hierarchical data rows with amounts
    """
    c = await get_client()

    from client import REPORT_ENDPOINTS

    if operation not in REPORT_ENDPOINTS:
        valid = ", ".join(sorted(REPORT_ENDPOINTS.keys()))
        return _json({"error": f"Unknown report: {operation}. Valid: {valid}"})

    # Aging reports use `report_date` instead of start_date/end_date
    AGING_REPORTS = {
        "ar_aging_summary", "ar_aging_detail",
        "ap_aging_summary", "ap_aging_detail",
    }

    params = {}
    if operation in AGING_REPORTS:
        # QBO aging API expects `report_date`, not start_date/end_date
        if end_date:
            params["report_date"] = end_date
        elif start_date:
            params["report_date"] = start_date
    else:
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

    if accounting_method:
        params["accounting_method"] = accounting_method
    if department:
        params["department"] = department
    if class_id:
        params["class"] = class_id
    if customer_id:
        params["customer"] = customer_id
    if vendor_id:
        params["vendor"] = vendor_id

    return _json(await c.report_run(operation, params))


# ═══════════════════════════════════════════════════════════════════════
# Entry point — dual transport
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"

    if transport == "sse":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("PORT", "3075"))
        logger.info(f"Starting QuickBooks MCP Server on SSE port {port}")
        mcp.run(transport="sse", port=port)
    else:
        logger.info("Starting QuickBooks MCP Server on STDIO")
        mcp.run(transport="stdio")
