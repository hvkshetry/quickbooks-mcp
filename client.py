"""QuickBooks Online client wrapper with token persistence and async safety.

Lazy singleton pattern matching inventree-mcp/client.py.
All python-quickbooks calls go through asyncio.to_thread() since the library is synchronous.
Token refresh is locked to prevent concurrent refresh races.
"""

import asyncio
import os
import re
import time
import logging

from intuitlib.client import AuthClient
from intuitlib.exceptions import AuthClientError
from quickbooks import QuickBooks
from quickbooks.exceptions import AuthorizationException, QuickbooksException
from quickbooks.objects.account import Account
from quickbooks.objects.customer import Customer
from quickbooks.objects.vendor import Vendor
from quickbooks.objects.employee import Employee
from quickbooks.objects.item import Item
from quickbooks.objects.invoice import Invoice
from quickbooks.objects.bill import Bill
from quickbooks.objects.billpayment import BillPayment
from quickbooks.objects.payment import Payment
from quickbooks.objects.deposit import Deposit
from quickbooks.objects.transfer import Transfer
from quickbooks.objects.journalentry import JournalEntry
from quickbooks.objects.purchase import Purchase
from quickbooks.objects.estimate import Estimate
from quickbooks.objects.creditmemo import CreditMemo
from quickbooks.objects.salesreceipt import SalesReceipt
from quickbooks.objects.refundreceipt import RefundReceipt
from quickbooks.objects.vendorcredit import VendorCredit
from quickbooks.objects.taxcode import TaxCode
from quickbooks.objects.department import Department
from quickbooks.objects.term import Term
from quickbooks.objects.paymentmethod import PaymentMethod
from quickbooks.objects.company_info import CompanyInfo
from quickbooks.objects.preferences import Preferences

logger = logging.getLogger(__name__)

# ── Async helper ─────────────────────────────────────────────────────

async def run_sync(fn, *args, **kwargs):
    """Run a synchronous function in a thread pool."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ── Token persistence ────────────────────────────────────────────────

def _persist_token(refresh_token: str, realm_id: str = None):
    """Atomically update refresh token (and optionally realm_id) in .env file."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    if re.search(r"^QBO_REFRESH_TOKEN=", content, re.MULTILINE):
        content = re.sub(
            r"^QBO_REFRESH_TOKEN=.*$",
            f"QBO_REFRESH_TOKEN={refresh_token}",
            content,
            flags=re.MULTILINE,
        )
    else:
        content += f"\nQBO_REFRESH_TOKEN={refresh_token}\n"

    if realm_id:
        if re.search(r"^QBO_REALM_ID=", content, re.MULTILINE):
            content = re.sub(
                r"^QBO_REALM_ID=.*$",
                f"QBO_REALM_ID={realm_id}",
                content,
                flags=re.MULTILINE,
            )
        else:
            content += f"QBO_REALM_ID={realm_id}\n"

    # Atomic write via temp file
    tmp_path = env_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, env_path)
    logger.info("Persisted new refresh token to .env")


# ── Entity type maps ─────────────────────────────────────────────────

TRANSACTION_TYPES = {
    "invoice": Invoice,
    "bill": Bill,
    "bill_payment": BillPayment,
    "payment": Payment,
    "deposit": Deposit,
    "transfer": Transfer,
    "journal_entry": JournalEntry,
    "purchase": Purchase,
    "estimate": Estimate,
    "credit_memo": CreditMemo,
    "sales_receipt": SalesReceipt,
    "refund_receipt": RefundReceipt,
    "vendor_credit": VendorCredit,
}

# Operations supported per entity type (not all support delete/void)
TRANSACTION_OPS = {
    "invoice": {"list", "get", "create", "update", "delete", "void", "search"},
    "bill": {"list", "get", "create", "update", "delete", "search"},
    "bill_payment": {"list", "get", "create", "update", "delete", "search"},
    "payment": {"list", "get", "create", "update", "delete", "void", "search"},
    "deposit": {"list", "get", "create", "update", "delete", "search"},
    "transfer": {"list", "get", "create", "update", "delete", "search"},
    "journal_entry": {"list", "get", "create", "update", "delete", "search"},
    "purchase": {"list", "get", "create", "update", "delete", "search"},
    "estimate": {"list", "get", "create", "update", "delete", "search"},
    "credit_memo": {"list", "get", "create", "update", "delete", "void", "search"},
    "sales_receipt": {"list", "get", "create", "update", "delete", "void", "search"},
    "refund_receipt": {"list", "get", "create", "update", "delete", "search"},
    "vendor_credit": {"list", "get", "create", "update", "delete", "search"},
}

PARTY_TYPES = {
    "customer": Customer,
    "vendor": Vendor,
    "employee": Employee,
}

REPORT_ENDPOINTS = {
    "profit_and_loss": "ProfitAndLoss",
    "balance_sheet": "BalanceSheet",
    "trial_balance": "TrialBalance",
    "cash_flow": "CashFlow",
    "general_ledger": "GeneralLedger",
    "ar_aging_summary": "AgedReceivables",
    "ar_aging_detail": "AgedReceivableDetail",
    "ap_aging_summary": "AgedPayables",
    "ap_aging_detail": "AgedPayableDetail",
    "customer_balance": "CustomerBalance",
    "vendor_balance": "VendorBalance",
}

REFERENCE_TYPES = {
    "list_tax_codes": TaxCode,
    "list_departments": Department,
    "list_terms": Term,
    "list_payment_methods": PaymentMethod,
}


# ── QBO Client ───────────────────────────────────────────────────────

class QBOClient:
    """QuickBooks Online client with automatic token refresh and async safety."""

    def __init__(self):
        self._refresh_lock = asyncio.Lock()
        self._last_refresh = 0
        self._build_session()

    def _build_session(self):
        """Build or rebuild the QBO session from environment variables."""
        self.auth_client = AuthClient(
            client_id=os.getenv("QBO_CLIENT_ID"),
            client_secret=os.getenv("QBO_CLIENT_SECRET"),
            environment=os.getenv("QBO_ENVIRONMENT", "sandbox"),
            redirect_uri=os.getenv("QBO_REDIRECT_URI", "http://localhost:8010/callback"),
        )
        refresh_token = os.getenv("QBO_REFRESH_TOKEN")
        realm_id = os.getenv("QBO_REALM_ID")

        if not refresh_token or not realm_id:
            raise RuntimeError(
                "QBO_REFRESH_TOKEN and QBO_REALM_ID must be set. "
                "Run `uv run python auth_flow.py` to complete OAuth setup."
            )

        # Exchange refresh token for new access token
        self.auth_client.refresh(refresh_token=refresh_token)
        self._last_refresh = time.time()

        self.qb = QuickBooks(
            auth_client=self.auth_client,
            refresh_token=refresh_token,
            company_id=realm_id,
        )
        logger.info(f"QBO session built for realm {realm_id}")

    async def _call(self, fn, *args, **kwargs):
        """Execute a QBO API call with automatic token refresh on 401."""
        try:
            return await run_sync(fn, *args, **kwargs)
        except (AuthorizationException, AuthClientError):
            logger.warning("QBO auth expired, refreshing token...")
            async with self._refresh_lock:
                # Only refresh if another coroutine hasn't already done it
                if time.time() - self._last_refresh < 5:
                    logger.info("Token was just refreshed by another call, retrying")
                else:
                    try:
                        await run_sync(
                            self.auth_client.refresh,
                            refresh_token=os.getenv("QBO_REFRESH_TOKEN"),
                        )
                        new_rt = self.auth_client.refresh_token
                        _persist_token(new_rt)
                        os.environ["QBO_REFRESH_TOKEN"] = new_rt
                        await run_sync(self._build_session)
                    except Exception as e:
                        raise RuntimeError(
                            f"Token refresh failed: {e}. "
                            "Re-run `uv run python auth_flow.py` to re-authorize."
                        ) from e
            # Retry once after refresh
            return await run_sync(fn, *args, **kwargs)

    async def _call_with_backoff(self, fn, *args, **kwargs):
        """Execute with rate-limit backoff (429 retry)."""
        for attempt in range(3):
            try:
                return await self._call(fn, *args, **kwargs)
            except QuickbooksException as e:
                if "429" in str(e) or "throttl" in str(e).lower():
                    wait = 2 ** attempt
                    logger.warning(f"Rate limited (429), backing off {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                raise
        raise RuntimeError("Rate limit exceeded after 3 retries")

    # ── Serialization ────────────────────────────────────────────────

    def _to_dict(self, obj) -> dict:
        """Convert a python-quickbooks object to a dict."""
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        elif hasattr(obj, "__dict__"):
            return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        return {"value": str(obj)}

    def _to_list(self, objects) -> list[dict]:
        """Convert a list of python-quickbooks objects to dicts."""
        return [self._to_dict(o) for o in objects]

    # ── Account operations ───────────────────────────────────────────

    async def account_list(self, limit=100, offset=0, account_type=None) -> list[dict]:
        where = f"AccountType = '{account_type}'" if account_type else None
        if where:
            result = await self._call_with_backoff(
                Account.where, where, qb=self.qb, max_results=limit, start_position=offset + 1
            )
        else:
            result = await self._call_with_backoff(
                Account.all, qb=self.qb, max_results=limit, start_position=offset + 1
            )
        return self._to_list(result)

    async def account_get(self, account_id: int) -> dict:
        result = await self._call_with_backoff(Account.get, account_id, qb=self.qb)
        return self._to_dict(result)

    async def account_create(self, data: dict) -> dict:
        acct = Account()
        for k, v in data.items():
            setattr(acct, k, v)
        result = await self._call_with_backoff(acct.save, qb=self.qb)
        return self._to_dict(result)

    async def account_update(self, account_id: int, data: dict) -> dict:
        acct = await self._call_with_backoff(Account.get, account_id, qb=self.qb)
        for k, v in data.items():
            setattr(acct, k, v)
        result = await self._call_with_backoff(acct.save, qb=self.qb)
        return self._to_dict(result)

    async def account_deactivate(self, account_id: int) -> dict:
        acct = await self._call_with_backoff(Account.get, account_id, qb=self.qb)
        acct.Active = False
        result = await self._call_with_backoff(acct.save, qb=self.qb)
        return self._to_dict(result)

    async def account_search(self, query: str, limit=100) -> list[dict]:
        where = f"Name LIKE '%{query}%'"
        result = await self._call_with_backoff(
            Account.where, where, qb=self.qb, max_results=limit
        )
        return self._to_list(result)

    # ── Party operations (Customer / Vendor / Employee) ──────────────

    async def party_list(self, party_type: str, limit=100, offset=0) -> list[dict]:
        cls = PARTY_TYPES[party_type]
        result = await self._call_with_backoff(
            cls.all, qb=self.qb, max_results=limit, start_position=offset + 1
        )
        return self._to_list(result)

    async def party_get(self, party_type: str, party_id: int) -> dict:
        cls = PARTY_TYPES[party_type]
        result = await self._call_with_backoff(cls.get, party_id, qb=self.qb)
        return self._to_dict(result)

    async def party_create(self, party_type: str, data: dict) -> dict:
        cls = PARTY_TYPES[party_type]
        obj = cls()
        for k, v in data.items():
            setattr(obj, k, v)
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def party_update(self, party_type: str, party_id: int, data: dict) -> dict:
        cls = PARTY_TYPES[party_type]
        obj = await self._call_with_backoff(cls.get, party_id, qb=self.qb)
        for k, v in data.items():
            setattr(obj, k, v)
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def party_deactivate(self, party_type: str, party_id: int) -> dict:
        cls = PARTY_TYPES[party_type]
        obj = await self._call_with_backoff(cls.get, party_id, qb=self.qb)
        obj.Active = False
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def party_search(self, party_type: str, query: str, limit=100) -> list[dict]:
        cls = PARTY_TYPES[party_type]
        where = f"DisplayName LIKE '%{query}%'"
        result = await self._call_with_backoff(
            cls.where, where, qb=self.qb, max_results=limit
        )
        return self._to_list(result)

    # ── Transaction operations ───────────────────────────────────────

    async def transaction_list(self, entity_type: str, limit=100, offset=0) -> list[dict]:
        cls = TRANSACTION_TYPES[entity_type]
        result = await self._call_with_backoff(
            cls.all, qb=self.qb, max_results=limit, start_position=offset + 1
        )
        return self._to_list(result)

    async def transaction_get(self, entity_type: str, entity_id: int) -> dict:
        cls = TRANSACTION_TYPES[entity_type]
        result = await self._call_with_backoff(cls.get, entity_id, qb=self.qb)
        return self._to_dict(result)

    async def transaction_create(self, entity_type: str, data: dict) -> dict:
        cls = TRANSACTION_TYPES[entity_type]
        obj = cls()
        for k, v in data.items():
            setattr(obj, k, v)
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def transaction_update(self, entity_type: str, entity_id: int, data: dict) -> dict:
        cls = TRANSACTION_TYPES[entity_type]
        obj = await self._call_with_backoff(cls.get, entity_id, qb=self.qb)
        for k, v in data.items():
            setattr(obj, k, v)
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def transaction_delete(self, entity_type: str, entity_id: int) -> dict:
        cls = TRANSACTION_TYPES[entity_type]
        obj = await self._call_with_backoff(cls.get, entity_id, qb=self.qb)
        result = await self._call_with_backoff(obj.delete, qb=self.qb)
        return {"deleted": True, "entity_type": entity_type, "id": entity_id}

    async def transaction_void(self, entity_type: str, entity_id: int) -> dict:
        cls = TRANSACTION_TYPES[entity_type]
        obj = await self._call_with_backoff(cls.get, entity_id, qb=self.qb)
        result = await self._call_with_backoff(obj.void, qb=self.qb)
        return {"voided": True, "entity_type": entity_type, "id": entity_id}

    async def transaction_search(self, entity_type: str, query: str, limit=100) -> list[dict]:
        cls = TRANSACTION_TYPES[entity_type]
        result = await self._call_with_backoff(
            cls.where, query, qb=self.qb, max_results=limit
        )
        return self._to_list(result)

    # ── Item operations ──────────────────────────────────────────────

    async def item_list(self, limit=100, offset=0, item_type=None) -> list[dict]:
        if item_type:
            where = f"Type = '{item_type.capitalize()}'"
            result = await self._call_with_backoff(
                Item.where, where, qb=self.qb, max_results=limit, start_position=offset + 1
            )
        else:
            result = await self._call_with_backoff(
                Item.all, qb=self.qb, max_results=limit, start_position=offset + 1
            )
        return self._to_list(result)

    async def item_get(self, item_id: int) -> dict:
        result = await self._call_with_backoff(Item.get, item_id, qb=self.qb)
        return self._to_dict(result)

    async def item_create(self, data: dict) -> dict:
        obj = Item()
        for k, v in data.items():
            setattr(obj, k, v)
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def item_update(self, item_id: int, data: dict) -> dict:
        obj = await self._call_with_backoff(Item.get, item_id, qb=self.qb)
        for k, v in data.items():
            setattr(obj, k, v)
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def item_deactivate(self, item_id: int) -> dict:
        obj = await self._call_with_backoff(Item.get, item_id, qb=self.qb)
        obj.Active = False
        result = await self._call_with_backoff(obj.save, qb=self.qb)
        return self._to_dict(result)

    async def item_search(self, query: str, limit=100) -> list[dict]:
        where = f"Name LIKE '%{query}%'"
        result = await self._call_with_backoff(
            Item.where, where, qb=self.qb, max_results=limit
        )
        return self._to_list(result)

    # ── Reference operations ─────────────────────────────────────────

    async def reference_list(self, operation: str) -> list[dict]:
        if operation in REFERENCE_TYPES:
            cls = REFERENCE_TYPES[operation]
            result = await self._call_with_backoff(cls.all, qb=self.qb, max_results=1000)
            return self._to_list(result)
        elif operation == "list_classes":
            # Classes use the Class entity
            from quickbooks.objects.trackingclass import Class as QBClass
            result = await self._call_with_backoff(QBClass.all, qb=self.qb, max_results=1000)
            return self._to_list(result)
        else:
            raise ValueError(f"Unknown reference operation: {operation}")

    async def reference_get_company_info(self) -> dict:
        result = await self._call_with_backoff(
            CompanyInfo.all, qb=self.qb, max_results=1
        )
        return self._to_dict(result[0]) if result else {}

    async def reference_get_preferences(self) -> dict:
        result = await self._call_with_backoff(
            Preferences.get, qb=self.qb
        )
        return self._to_dict(result) if result else {}

    # ── Report operations ────────────────────────────────────────────

    async def report_run(self, report_type: str, params: dict) -> dict:
        endpoint = REPORT_ENDPOINTS[report_type]
        # Build query params, filtering out None values
        query_params = {k: v for k, v in params.items() if v is not None}
        # Use the library's get_report method which handles URL + auth correctly
        result = await self._call_with_backoff(
            self.qb.get_report, endpoint, qs=query_params
        )
        return result if isinstance(result, dict) else {"data": str(result)}
