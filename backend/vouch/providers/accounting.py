"""Where approved customers go: a local ledger file, or QuickBooks Online.

Note on QuickBooks: the QBO API cannot read or write a customer's credit limit
field, so Vouch sets the payment terms (SalesTermRef) and records the approved
limit and memo link in the customer's Notes.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlencode

import httpx

from ..db import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..services import Services

QBO_AUTHORIZE = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPE = "com.intuit.quickbooks.accounting"
QBO_MINOR = "75"


class Accounting:
    mode = "base"

    def __init__(self, services: "Services") -> None:
        self.s = services

    def status(self) -> dict:
        return {"mode": self.mode, "connected": True}

    async def sync_customer(self, customer: dict) -> dict:
        raise NotImplementedError


class LedgerAccounting(Accounting):
    """Appends approved customers to data/ledger.json. Stands in for an ERP."""

    mode = "ledger"

    async def sync_customer(self, customer: dict) -> dict:
        path = self.s.settings.data_dir / "ledger.json"
        entries = json.loads(path.read_text()) if path.exists() else []
        customer_id = f"CUST-{len(entries) + 1:04d}"
        entry = {"customer_id": customer_id, "synced_at": utcnow().isoformat(), **customer}
        entries.append(entry)
        path.write_text(json.dumps(entries, indent=2))
        return {"ok": True, "provider": "ledger", "customer_id": customer_id, "url": None, "payload": entry}


class QuickBooksAccounting(Accounting):
    mode = "quickbooks"

    @property
    def api_base(self) -> str:
        host = "sandbox-quickbooks.api.intuit.com" if self.s.settings.quickbooks_environment == "sandbox" else "quickbooks.api.intuit.com"
        return f"https://{host}/v3/company"

    def redirect_uri(self) -> str:
        base = (self.s.settings.public_base_url or "http://localhost:8000").rstrip("/")
        return f"{base}/api/integrations/quickbooks/callback"

    def tokens(self) -> Optional[dict]:
        return self.s.db.kv_get("quickbooks_tokens")

    def status(self) -> dict:
        t = self.tokens()
        return {"mode": self.mode, "connected": bool(t), "realm_id": t.get("realm_id") if t else None, "environment": self.s.settings.quickbooks_environment}

    def authorize_url(self) -> str:
        state = secrets.token_urlsafe(16)
        self.s.db.kv_set("quickbooks_oauth_state", state)
        params = {
            "client_id": self.s.settings.quickbooks_client_id,
            "response_type": "code",
            "scope": QBO_SCOPE,
            "redirect_uri": self.redirect_uri(),
            "state": state,
        }
        return f"{QBO_AUTHORIZE}?{urlencode(params)}"

    async def complete_oauth(self, code: str, realm_id: str, state: str) -> None:
        if state != self.s.db.kv_get("quickbooks_oauth_state"):
            raise ValueError("OAuth state mismatch")
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri()}
        tokens = await self._token_request(data)
        tokens["realm_id"] = realm_id
        self.s.db.kv_set("quickbooks_tokens", tokens)

    async def _token_request(self, data: dict) -> dict:
        auth = (self.s.settings.quickbooks_client_id or "", self.s.settings.quickbooks_client_secret or "")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(QBO_TOKEN, data=data, auth=auth, headers={"Accept": "application/json"})
        resp.raise_for_status()
        body = resp.json()
        body["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 60
        return body

    async def _access_token(self) -> tuple[str, str]:
        t = self.tokens()
        if not t:
            raise RuntimeError("QuickBooks is not connected. Open Settings and connect a sandbox company.")
        if time.time() >= t.get("expires_at", 0):
            refreshed = await self._token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
            refreshed["realm_id"] = t["realm_id"]
            self.s.db.kv_set("quickbooks_tokens", refreshed)
            t = refreshed
        return t["access_token"], t["realm_id"]

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        token, realm = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}
        url = f"{self.api_base}/{realm}/{path}"
        params = {"minorversion": QBO_MINOR, **kwargs.pop("params", {})}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(method, url, headers=headers, params=params, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"QuickBooks {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _term_id(self, terms: str) -> Optional[str]:
        safe = terms.replace("'", "")
        body = await self._request("GET", "query", params={"query": f"select * from Term where Name = '{safe}'"})
        found = body.get("QueryResponse", {}).get("Term", [])
        return found[0]["Id"] if found else None

    async def sync_customer(self, customer: dict) -> dict:
        terms_id = await self._term_id(customer["terms"])
        addr = customer.get("address") or {}
        payload: dict = {
            "DisplayName": customer["display_name"],
            "CompanyName": customer["legal_name"],
            "Notes": (
                f"Credit approved by Vouch on {datetime.now(timezone.utc).date().isoformat()}: "
                f"${customer['credit_limit']:,.0f} limit on {customer['terms']}. Memo: {customer.get('memo_url', '')}"
            )[:2000],
            "BillAddr": {
                "Line1": addr.get("street"),
                "City": addr.get("city"),
                "CountrySubDivisionCode": addr.get("state"),
                "PostalCode": addr.get("postal_code"),
            },
        }
        if customer.get("email"):
            payload["PrimaryEmailAddr"] = {"Address": customer["email"]}
        if customer.get("phone"):
            payload["PrimaryPhone"] = {"FreeFormNumber": customer["phone"]}
        if terms_id:
            payload["SalesTermRef"] = {"value": terms_id}
        body = await self._request("POST", "customer", json=payload)
        cust = body.get("Customer", {})
        host = "app.sandbox.qbo.intuit.com" if self.s.settings.quickbooks_environment == "sandbox" else "app.qbo.intuit.com"
        return {
            "ok": True,
            "provider": "quickbooks",
            "customer_id": cust.get("Id"),
            "url": f"https://{host}/app/customerdetail?nameId={cust.get('Id')}",
            "terms_matched": bool(terms_id),
            "payload": payload,
        }


def build_accounting(services: "Services") -> Accounting:
    st = services.settings
    if st.accounting_mode == "quickbooks" and st.quickbooks_client_id and st.quickbooks_client_secret:
        return QuickBooksAccounting(services)
    return LedgerAccounting(services)
