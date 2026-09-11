"""BOM enrichment / stock check / order-list generation."""
from __future__ import annotations

import csv
from pathlib import Path

from . import digikey_api as dk
from .config import Settings
from .kicad_bom import row_search_text


def enrich_bom(
    rows: list[dict],
    settings: Settings,
    limit: int = 3,
    delay_s: float = 0.3,
) -> list[dict]:
    import time

    enriched: list[dict] = []
    for row in rows:
        query = row_search_text(row)
        out = dict(row)
        if not query:
            out.update({"dk_status": "skipped", "dk_error": "empty query"})
            enriched.append(out)
            continue
        try:
            data = dk.keyword_search(settings, query, limit=limit)
            products = data.get("Products", []) or []
            if not products:
                out.update({"dk_status": "not_found"})
            else:
                best = dk.simplify_product(products[0])
                out.update(
                    {
                        "digikey_pn": best["digikey_pn"],
                        "dk_mpn": best["mpn"],
                        "dk_manufacturer": best["manufacturer"],
                        "dk_description": best["description"],
                        "dk_datasheet": best["datasheet_url"],
                        "dk_product_url": best["product_url"],
                        "dk_stock": best["quantity_available"],
                        "dk_unit_price": str(best["unit_price"] or ""),
                        "dk_status": "found",
                        "dk_candidates": len(products),
                    }
                )
            time.sleep(delay_s)  # be nice to the API
        except Exception as e:  # keep BOM usable even on API errors
            out.update({"dk_status": "error", "dk_error": str(e)[:300]})
        enriched.append(out)
    return enriched


def check_stock(rows: list[dict], settings: Settings) -> list[dict]:
    """For rows that already carry a digikey_pn, fetch live ProductDetails stock."""
    results: list[dict] = []
    for row in rows:
        dkpn = (row.get("digikey_pn") or row.get("Digikey_PN") or row.get("DK_PN") or "").strip()
        query = dkpn or row_search_text(row)
        if not query:
            results.append({**row, "stock_status": "skipped"})
            continue
        try:
            if dkpn:
                try:
                    details = dk.product_details(settings, dkpn)
                    prod = details.get("Product", details)
                    s = dk.simplify_product(prod)
                    results.append(
                        {
                            **row,
                            "digikey_pn": s["digikey_pn"] or dkpn,
                            "stock": s["quantity_available"],
                            "stock_status": s["stock_status"],
                            "unit_price": str(s["unit_price"] or ""),
                            "datasheet": s["datasheet_url"],
                            "ok": (s["quantity_available"] or 0) > 0,
                        }
                    )
                    continue
                except Exception:
                    pass  # fall back to keyword search
            data = dk.keyword_search(settings, query, limit=1)
            prods = data.get("Products", []) or []
            if not prods:
                results.append({**row, "stock_status": "not_found", "ok": False})
            else:
                s = dk.simplify_product(prods[0])
                results.append(
                    {
                        **row,
                        "digikey_pn": s["digikey_pn"],
                        "stock": s["quantity_available"],
                        "stock_status": s["stock_status"],
                        "unit_price": str(s["unit_price"] or ""),
                        "datasheet": s["datasheet_url"],
                        "ok": (s["quantity_available"] or 0) > 0,
                    }
                )
        except Exception as e:
            results.append({**row, "stock_status": f"error: {e}"[:200], "ok": False})
    return results


def write_csv(rows: list[dict], path: Path) -> Path:
    if not rows:
        raise ValueError("No rows to write")
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


def build_order_list(rows: list[dict], qty_field: str = "Qty") -> list[dict]:
    """Collapse an enriched BOM into a DigiKey-uploadable order list.

    Output columns match DigiKey's MyLists/BOM-Manager CSV import:
    Quantity, DigiKey Part Number, Manufacturer Part Number, Reference(s).
    """
    order: list[dict] = []
    for r in rows:
        dkpn = (r.get("digikey_pn") or r.get("Digikey_PN") or r.get("DK_PN") or "").strip()
        if not dkpn:
            continue
        qty = r.get(qty_field) or r.get("Quantity") or r.get("Qty") or r.get("QUANTITY") or "1"
        try:
            qty_n = int(str(qty).strip().split()[0])
        except Exception:
            qty_n = 1
        order.append(
            {
                "Quantity": qty_n,
                "DigiKey Part Number": dkpn,
                "Manufacturer Part Number": r.get("dk_mpn") or r.get("MPN") or "",
                "Reference": r.get("Reference") or r.get("Refs") or "",
                "Value": r.get("Value") or "",
                "Description": r.get("dk_description") or r.get("Description") or "",
                "Unit Price": r.get("dk_unit_price") or r.get("unit_price") or "",
                "Product URL": r.get("dk_product_url") or r.get("Product URL") or "",
            }
        )
    # merge duplicate DKPNS
    merged: dict[str, dict] = {}
    for line in order:
        k = line["DigiKey Part Number"]
        if k in merged:
            merged[k]["Quantity"] += line["Quantity"]
            merged[k]["Reference"] += f",{line['Reference']}" if line["Reference"] else ""
        else:
            merged[k] = dict(line)
    return list(merged.values())
