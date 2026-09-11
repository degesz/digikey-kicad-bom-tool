"""BOM enrichment / stock check / order-list generation."""
from __future__ import annotations

import csv
from pathlib import Path

from . import digikey_api as dk
from .config import Settings
from .kicad_bom import candidate_queries


def _row_qty(row: dict) -> int:
    for k in ("Qty", "QTY", "Quantity", "QUANTITY"):
        try:
            return max(1, int(str(row.get(k, "")).strip().split()[0]))
        except (ValueError, IndexError):
            continue
    return 1


def _price_of(product: dict) -> float:
    try:
        return float(product.get("UnitPrice") or float("inf"))
    except (TypeError, ValueError):
        return float("inf")


def rank_products(products: list[dict], row_mpn: str = "") -> list[dict]:
    """Best candidate first.

    - Exact MPN match always wins (row carries a real manufacturer part number).
    - Otherwise prefer large stock + low single-unit price (generic passives).
    """
    mpn = (row_mpn or "").strip().upper()

    def key(p: dict):
        exact = 0
        if mpn and (p.get("ManufacturerProductNumber") or "").strip().upper() == mpn:
            exact = 0
        else:
            exact = 1 if mpn else 0  # no MPN -> all generic, rank by availability
        stock = p.get("QuantityAvailable") or 0
        return (exact, 0 if stock > 0 else 1, _price_of(p), -stock)

    return sorted(products, key=key)


def choose_variant(variations: list[dict], needed: int = 1) -> dict | None:
    """Pick the order packaging. Cut tape accepted: prefer prototype-friendly
    in-stock packaging (CT, Digi-Reel) with the lowest MOQ covering `needed`."""
    in_stock = [v for v in variations if (v.get("stock") or 0) > 0]
    if not in_stock:
        return None

    def key(v: dict):
        name = v.get("packaging") or ""
        pref = 0 if "Cut Tape" in name else (1 if "Digi-Reel" in name or "DigiReel" in name else 2)
        return (pref, v.get("moq") or 0, -(v.get("stock") or 0))

    cover = [v for v in in_stock if (v.get("stock") or 0) >= needed]
    return sorted(cover or in_stock, key=key)[0]


def alternates_text(products: list[dict]) -> str:
    bits = []
    for p in products[:3]:
        sp = dk.simplify_product(p)
        var = (sp.get("variations") or [{}])[0]
        bits.append(
            f"{var.get('digikey_pn') or sp['digikey_pn']} "
            f"({sp['mpn']}; stock {sp['quantity_available']}; ${sp['unit_price']})"
        )
    return "; ".join(bits)


def enrich_bom(
    rows: list[dict],
    settings: Settings,
    limit: int = 5,
    delay_s: float = 0.3,
) -> list[dict]:
    import time

    enriched: list[dict] = []
    for row in rows:
        queries = candidate_queries(row)
        out = dict(row)
        if not queries:
            out.update({"dk_status": "skipped", "dk_error": "empty query"})
            enriched.append(out)
            continue
        try:
            data: dict | None = None
            used = ""
            tried: list[str] = []
            for q in queries[:4]:
                tried.append(q)
                data = dk.keyword_search(settings, q, limit=limit)
                if data.get("Products"):
                    used = q
                    break
                time.sleep(delay_s)
            products = (data.get("Products", []) if data else []) or []
            out["dk_tried"] = " | ".join(tried)
            if not products:
                out.update({"dk_status": "not_found", "dk_query": ""})
            else:
                row_mpn = (row.get("MPN") or row.get("mpn") or "").strip()
                ranked = rank_products(products, row_mpn)
                best = dk.simplify_product(ranked[0])
                needed = _row_qty(row)
                chosen = choose_variant(best.get("variations") or [], needed)
                base = {
                    "dk_mpn": best["mpn"],
                    "dk_manufacturer": best["manufacturer"],
                    "dk_description": best["description"],
                    "dk_datasheet": best["datasheet_url"],
                    "dk_product_url": best["product_url"],
                    "dk_unit_price": str(best["unit_price"] or ""),
                    "dk_candidates": len(products),
                    "dk_query": used,
                }
                if chosen and (chosen.get("stock") or 0) >= needed:
                    out.update(
                        base
                        | {
                            "digikey_pn": chosen["digikey_pn"],
                            "dk_pkg": chosen.get("packaging"),
                            "dk_moq": chosen.get("moq"),
                            "dk_stock": chosen.get("stock"),
                            "dk_status": "found",
                        }
                    )
                elif chosen:
                    out.update(
                        base
                        | {
                            "digikey_pn": chosen["digikey_pn"],
                            "dk_pkg": chosen.get("packaging"),
                            "dk_moq": chosen.get("moq"),
                            "dk_stock": chosen.get("stock"),
                            "dk_status": "needs_review",
                            "dk_review_reason": f"only {chosen['stock']} in stock, need {needed}",
                        }
                    )
                else:
                    out.update(
                        base
                        | {
                            "digikey_pn": best["digikey_pn"],
                            "dk_pkg": (best.get("variations") or [{}])[0].get("packaging"),
                            "dk_stock": 0,
                            "dk_status": "needs_review",
                            "dk_review_reason": "out_of_stock_everywhere",
                            "dk_alternatives": alternates_text(ranked[1:]),
                        }
                    )
            time.sleep(delay_s)  # be nice to the API
        except Exception as e:  # keep BOM usable even on API errors
            out.update({"dk_status": "error", "dk_error": str(e)[:300]})
        enriched.append(out)
    return enriched


def check_stock(rows: list[dict], settings: Settings) -> list[dict]:
    """Live stock per row. Packaging doesn't matter (cut tape accepted):
    `ok` is true when ANY packaging variant has stock; `best_pkg` names the
    preferred orderable variant (prototype-friendly CT/Digi-Reel first)."""
    results: list[dict] = []
    for row in rows:
        dkpn = (row.get("digikey_pn") or row.get("Digikey_PN") or row.get("DK_PN") or "").strip()
        queries = candidate_queries(row) if not dkpn else [dkpn]
        query = queries[0] if queries else ""
        if not query:
            results.append({**row, "stock_status": "skipped"})
            continue
        try:
            if dkpn:
                try:
                    details = dk.product_details(settings, dkpn)
                    prod = details.get("Product", details)
                    s = dk.simplify_product(prod)
                    # Prefer the exact packaging variation's stock when present
                    stock = s["quantity_available"]
                    for v in s.get("variations") or []:
                        if (v.get("digikey_pn") or "").upper() == dkpn.upper():
                            stock = v.get("stock")
                            break
                    needed = _row_qty(row)
                    best = choose_variant(s.get("variations") or [], needed)
                    any_stock = max([(v.get("stock") or 0) for v in (s.get("variations") or [])] + [0])
                    results.append(
                        {
                            **row,
                            "digikey_pn": s["digikey_pn"] or dkpn,
                            "stock": stock,
                            "stock_any_pkg": any_stock,
                            "best_pkg": (best or {}).get("digikey_pn", ""),
                            "best_pkg_name": (best or {}).get("packaging", ""),
                            "stock_status": s["stock_status"],
                            "unit_price": str(s["unit_price"] or ""),
                            "datasheet": s["datasheet_url"],
                            "ok": any_stock > 0,
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
                any_stock = s["quantity_available"] or 0
                results.append(
                    {
                        **row,
                        "digikey_pn": s["digikey_pn"],
                        "stock": any_stock,
                        "stock_any_pkg": any_stock,
                        "best_pkg": s["digikey_pn"],
                        "unit_price": str(s["unit_price"] or ""),
                        "datasheet": s["datasheet_url"],
                        "ok": any_stock > 0,
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


def build_order_list(rows: list[dict], qty_field: str = "Qty") -> tuple[list[dict], list[str]]:
    """Collapse an enriched BOM into a DigiKey-uploadable order list.

    Output columns match DigiKey's MyLists/BOM-Manager CSV import:
    Quantity, DigiKey Part Number, Manufacturer Part Number, Reference(s).
    Rows flagged needs_review/not_found/error (no orderable pick) are skipped;
    their references are returned second for the caller to report.
    Returns (order_lines, skipped_references).
    """
    order: list[dict] = []
    skipped: list[str] = []
    for r in rows:
        status = (r.get("dk_status") or "").strip()
        if status and status not in ("found",):
            skipped.append(r.get("Reference") or r.get("Refs") or "?")
            continue
        dkpn = (r.get("digikey_pn") or r.get("Digikey_PN") or r.get("DK_PN") or "").strip()
        if not dkpn:
            skipped.append(r.get("Reference") or r.get("Refs") or "?")
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
    merged: dict[str, dict] = {}
    for line in order:
        k = line["DigiKey Part Number"]
        if k in merged:
            merged[k]["Quantity"] += line["Quantity"]
            merged[k]["Reference"] += f",{line['Reference']}" if line["Reference"] else ""
        else:
            merged[k] = dict(line)
    return list(merged.values()), skipped
