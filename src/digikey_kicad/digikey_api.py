"""DigiKey Product Information v4 client (OAuth2 client_credentials)."""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import requests

from .config import Settings, require_credentials

TOKEN_CACHE = Path.home() / ".cache" / "digikey-kicad" / "token.json"


def _load_cached_token(client_id: str) -> dict | None:
    try:
        if not TOKEN_CACHE.exists():
            return None
        data = json.loads(TOKEN_CACHE.read_text())
        if data.get("client_id") != client_id:
            return None
        if data.get("expires_at", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def _save_token(client_id: str, access_token: str, expires_in: int) -> None:
    try:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(
            json.dumps(
                {
                    "client_id": client_id,
                    "access_token": access_token,
                    "expires_at": time.time() + int(expires_in) - 60,
                }
            )
        )
    except Exception:
        pass


def get_access_token(settings: Settings) -> str:
    require_credentials(settings)
    cached = _load_cached_token(settings.client_id)
    if cached:
        return cached["access_token"]
    resp = requests.post(
        settings.token_url,
        data={
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token request failed HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Token endpoint returned no access_token: {data}")
    _save_token(settings.client_id, token, int(data.get("expires_in", 600)))
    return token


def _headers(settings: Settings, token: str) -> dict:
    return {
        "X-DIGIKEY-Client-Id": settings.client_id,
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Locale-Site": settings.site,
        "X-DIGIKEY-Locale-Language": settings.lang,
        "X-DIGIKEY-Locale-Currency": settings.currency,
        "X-DIGIKEY-Customer-Id": "0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def keyword_search(
    settings: Settings,
    keywords: str,
    limit: int = 10,
    offset: int = 0,
    in_stock_only: bool = False,
) -> dict:
    token = get_access_token(settings)
    body: dict = {"Keywords": keywords, "Limit": max(1, min(limit, 50)), "Offset": offset}
    resp = requests.post(
        settings.keyword_url, headers=_headers(settings, token), json=body, timeout=30
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Keyword search failed HTTP {resp.status_code}: {resp.text[:1000]}")
    data = resp.json()
    if in_stock_only:
        data["Products"] = [p for p in data.get("Products", []) if (p.get("QuantityAvailable") or 0) > 0]
    return data


def product_details(settings: Settings, part_number: str) -> dict:
    token = get_access_token(settings)
    resp = requests.get(
        settings.product_details_url(part_number),
        headers=_headers(settings, token),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ProductDetails failed HTTP {resp.status_code}: {resp.text[:1000]}")
    return resp.json()


def simplify_product(p: dict) -> dict:
    """Flatten the verbose v4 product into an agent-friendly record."""
    desc = p.get("Description", {}) or {}
    mfg = p.get("Manufacturer", {}) or {}
    cat = p.get("Category", {}) or {}
    params = p.get("Parameters", []) or []
    variations = p.get("ProductVariations", []) or []
    first_var = variations[0] if variations else {}
    dkpn = (
        p.get("DigiKeyPartNumber")
        or p.get("DigiKeyProductNumber")
        or first_var.get("DigiKeyProductNumber")
    )
    price_breaks = (
        p.get("StandardPricing") or p.get("ProductPricing")
        or first_var.get("StandardPricing") or []
    )
    status = p.get("ProductStatus")
    stock_status = p.get("StockStatus") or (status.get("Status") if isinstance(status, dict) else status)
    qty = p.get("QuantityAvailable")
    if qty is None and variations:
        # ProductDetails responses carry stock per packaging variation only
        qty = sum((v.get("QuantityAvailableforPackageType") or 0) for v in variations)
    flat_params = []
    for x in params:
        if not isinstance(x, dict):
            continue
        flat_params.append({
            "name": x.get("Parameter") or x.get("ParameterText"),
            "value": x.get("Value") or x.get("ValueText"),
        })
    return {
        "digikey_pn": dkpn,
        "mpn": p.get("ManufacturerProductNumber"),
        "manufacturer": mfg.get("Name"),
        "description": desc.get("ProductDescription"),
        "detailed_description": desc.get("DetailedDescription"),
        "datasheet_url": p.get("DatasheetUrl"),
        "product_url": p.get("ProductUrl"),
        "photo_url": p.get("PhotoUrl"),
        "category": cat.get("Name") if isinstance(cat, dict) else cat,
        "quantity_available": qty,
        "stock_status": stock_status,
        "unit_price": (p.get("UnitPrice") or None),
        "price_breaks": price_breaks,
        "variations": [
            {
                "digikey_pn": v.get("DigiKeyProductNumber"),
                "packaging": (v.get("PackageType") or {}).get("Name") if isinstance(v.get("PackageType"), dict) else v.get("PackageType"),
                "stock": v.get("QuantityAvailableforPackageType"),
                "moq": v.get("MinimumOrderQuantity"),
                "marketplace": bool(v.get("MarketPlace", False)),
            }
            for v in variations[:10]
        ],
        "parameters": flat_params[:25],
        "rohs": p.get("RohsStatus") or p.get("RohsInfo"),
        "packaging": (p.get("Packaging") or {}).get("Name") if isinstance(p.get("Packaging"), dict) else p.get("Packaging"),
    }
