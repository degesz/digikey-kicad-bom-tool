"""`dk` command interface — human-friendly tables, `--json` for AI agents."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import digikey_api as dk
from .bom_ops import build_order_list, check_stock, enrich_bom, write_csv
from .config import resolve_settings
from .kicad_bom import load_bom

app = typer.Typer(add_completion=False, help="DigiKey + KiCad BOM tool (agent-friendly CLI).")
bom_app = typer.Typer(help="BOM commands: load, enrich, stock, order lists.")
app.add_typer(bom_app, name="bom")
console = Console()

# ---------- global output helpers ----------

def emit(data, as_json: bool, table_fn=None):
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif table_fn:
        table_fn(data)
    else:
        print(json.dumps(data, indent=2, default=str))


def _common_options(func):
    return func


def _settings(
    client_id: Optional[str], client_secret: Optional[str], env: Optional[str],
    site: Optional[str], lang: Optional[str], currency: Optional[str],
):
    return resolve_settings(client_id, client_secret, env, site, lang, currency)


# ---------- auth ----------

@app.command()
def auth(
    client_id: Optional[str] = typer.Option(None, "--client-id", help="Override DigiKey client ID"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret", help="Override DigiKey client secret"),
    env: Optional[str] = typer.Option(None, "--env", help="production|sandbox"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output for agents"),
):
    """Verify credentials by fetching an OAuth2 token."""
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        token = dk.get_access_token(s)
        emit({"ok": True, "env": s.env, "token_prefix": token[:8] + "…"}, json_out)
    except Exception as e:
        emit({"ok": False, "error": str(e)}, True)
        raise typer.Exit(1)


@app.command()
def config_show(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """Show resolved config (secret redacted)."""
    s = resolve_settings()
    data = {
        "client_id": s.client_id[:6] + "…" if s.client_id else "",
        "client_secret_set": bool(s.client_secret),
        "env": s.env,
        "site": s.site,
        "lang": s.lang,
        "currency": s.currency,
        "base_url": s.base_url,
    }
    emit(data, json_out)


# ---------- search / details / stock ----------

@app.command()
def search(
    keywords: str = typer.Argument(..., help="Keyword / MPN / DigiKey PN to search"),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50),
    in_stock_only: bool = typer.Option(False, "--in-stock-only", help="Filter to stock > 0"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output for agents"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Search DigiKey parts by keyword."""
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        data = dk.keyword_search(s, keywords, limit=limit, in_stock_only=in_stock_only)
        products = [dk.simplify_product(p) for p in data.get("Products", [])]
        out = {"query": keywords, "count": len(products), "results": products}
    except Exception as e:
        out = {"query": keywords, "error": str(e)}
        emit(out, True)
        raise typer.Exit(1)

    def table(d):
        t = Table(title=f"DigiKey search: {d['query']} ({d['count']})")
        for c in ("digikey_pn", "mpn", "manufacturer", "description", "quantity_available"):
            t.add_column(c)
        for r in d["results"]:
            t.add_row(str(r["digikey_pn"]), str(r["mpn"]), str(r["manufacturer"]), str((r["description"] or "")[:60]), str(r["quantity_available"]))
        console.print(t)

    emit(out, json_out, table)


@app.command()
def details(
    part: str = typer.Argument(..., help="DigiKey part number (e.g. 311-1.0KFRCT-ND)"),
    json_out: bool = typer.Option(False, "--json"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Live product details (pricing, stock, datasheet) for one part."""
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        data = dk.product_details(s, part)
        prod = data.get("Product", data)
        emit(dk.simplify_product(prod), json_out)
    except Exception as e:
        emit({"part": part, "error": str(e)}, True)
        raise typer.Exit(1)


@app.command()
def stock(
    parts: list[str] = typer.Argument(..., help="One or more DigiKey PNs"),
    json_out: bool = typer.Option(False, "--json"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Check stock for part numbers (agent-friendly)."""
    s = _settings(client_id, client_secret, env, None, None, None)
    rows = check_stock([{"digikey_pn": p} for p in parts], s)
    emit({"results": rows}, json_out)


# ---------- bom ----------

@bom_app.command("from-project")
def bom_from_project(
    project: Path = typer.Argument(..., help="KiCad project folder, .csv/.xml BOM, or .kicad_sch"),
    json_out: bool = typer.Option(False, "--json"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write BOM CSV here"),
):
    """Resolve a KiCad 10 project folder to a BOM (uses kicad-cli if available)."""
    try:
        rows, provenance = load_bom(project)
        if out:
            write_csv(rows, out)
        emit({"provenance": provenance, "count": len(rows), "rows": rows, "out": str(out) if out else None}, json_out,
             lambda d: console.print(f"[green]{d['count']} rows[/green] via {d['provenance']}"))
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("enrich")
def bom_enrich(
    source: Path = typer.Argument(..., help="Project folder or BOM file"),
    out: Path = typer.Option(Path("enriched_bom.csv"), "--out", "-o"),
    limit: int = typer.Option(3, "--limit", help="Candidates fetched per row (1-10)"),
    refresh_ignored: bool = typer.Option(False, "--refresh-ignored", help="Re-process rows previously excluded"),
    json_out: bool = typer.Option(False, "--json"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Match each BOM row to DigiKey: adds DKPN + datasheet + description + stock + price."""
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, provenance = load_bom(source)
        enriched = enrich_bom(rows, s, limit=min(max(limit, 1), 10), reprocess_ignored=refresh_ignored)
        write_csv(enriched, out)
        found = sum(1 for r in enriched if r.get("dk_status") == "found")
        emit({"provenance": provenance, "total": len(enriched), "matched": found, "out": str(out), "rows": enriched},
             json_out, lambda d: console.print(f"[green]{d['matched']}/{d['total']}[/green] matched → {d['out']}"))
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("check-stock")
def bom_check_stock(
    source: Path = typer.Argument(..., help="Enriched BOM csv or project folder"),
    json_out: bool = typer.Option(False, "--json"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Check live stock for every row in a (possibly enriched) BOM."""
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, provenance = load_bom(source)
        results = check_stock(rows, s)
        if out:
            write_csv(results, out)
        active = [r for r in results if (r.get("dk_status") or "") != "ignored"]
        ignored = len(results) - len(active)
        ok = sum(1 for r in active if r.get("ok"))

        def table(d):
            t = Table(title=f"Stock: {ok}/{len(active)} in stock" + (f" ({ignored} excluded)" if ignored else ""))
            for c in ("Reference", "digikey_pn", "stock", "unit_price"):
                t.add_column(c)
            for r in active:
                t.add_row(str(r.get("Reference", "")), str(r.get("digikey_pn", "")), str(r.get("stock", "")), str(r.get("unit_price", "")))
            console.print(t)

        emit({"provenance": provenance, "in_stock": ok, "total": len(active), "ignored": ignored,
              "rows": results, "out": str(out) if out else None}, json_out, table)
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("order-list")
def bom_order_list(
    source: Path = typer.Argument(..., help="Enriched BOM csv or project folder"),
    out: Path = typer.Option(Path("order_list.csv"), "--out", "-o"),
    json_out: bool = typer.Option(False, "--json"),
    qty_field: str = typer.Option("Qty", "--qty-field"),
    include_oos: bool = typer.Option(False, "--include-oos", help="Include out-of-stock/needs-review rows that have a DKPN"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Build a DigiKey-compatible order list CSV (upload to MyLists/BOM Manager).

    Prefer `dk bom push-list`, which creates the DigiKey list directly.
    Excluded (ignored) rows and rows still needing a pick are skipped.
    """
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, provenance = load_bom(source)
        # auto-enrich if rows lack digikey_pn
        if rows and not any(r.get("digikey_pn") or r.get("Digikey_PN") for r in rows):
            console.print("[yellow]No DigiKey PNs found — enriching via API first…[/yellow]", file=sys.stderr)
            rows = enrich_bom(rows, s)
        order, skipped = build_order_list(rows, qty_field=qty_field, include_oos=include_oos)
        if not order:
            emit({"error": "No rows with DigiKey part numbers; enrich the BOM first.", "provenance": provenance}, True)
            raise typer.Exit(1)
        write_csv(order, out)
        emit({"provenance": provenance, "lines": len(order), "out": str(out),
              "skipped_needs_pick": skipped, "order": order}, json_out,
             lambda d: console.print(f"[green]{d['lines']} order lines → {d['out']}[/green]\nUpload at digikey.com → MyLists → Upload BOM." +
                                     (f"\n[yellow]Skipped (need picks): {', '.join(d['skipped_needs_pick'])}[/yellow]" if d["skipped_needs_pick"] else "")))
    except typer.Exit:
        raise
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("write-back")
def bom_write_back(
    enriched: Path = typer.Argument(..., help="Enriched BOM CSV (from `dk bom enrich`)"),
    project: Path = typer.Argument(..., help="KiCad project folder to annotate"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report matches without modifying files"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Write Digikey_PN + Datasheet + Digikey_URL fields back into the .kicad_sch symbols.

    Matches enriched rows to schematic symbols by Reference. Originals are
    backed up into <project>/.dk-backups/ before modification.
    """
    from .kicad_bom import expand_references, write_back_to_schematic

    try:
        rows, _prov = load_bom(enriched)
        ref_map: dict[str, dict] = {}
        for ref, r in expand_references(rows).items():
            status = (r.get("dk_status") or "").strip()
            if status == "ignored":
                continue  # excluded hardware: never touch the schematic
            dkpn = (r.get("digikey_pn") or "").strip()
            ds = (r.get("dk_datasheet") or "").strip()
            url = (r.get("dk_product_url") or r.get("Digikey_URL") or "").strip()
            if status in ("", "found"):
                # raw or confirmed rows may carry schematic-origin columns
                dkpn = dkpn or (r.get("Digikey_PN") or "").strip()
                ds = ds or (r.get("Datasheet") or r.get("datasheet") or "").strip()
            # needs_review/not_found/error: lowercase only, so cleared
            # fields are never resurrected from stale schematic columns
            if dkpn or ds or url:
                ref_map[ref] = {"digikey_pn": dkpn, "datasheet": ds, "url": url}
        if not ref_map:
            emit({"error": "Enriched BOM has no digikey_pn/datasheet values; run `dk bom enrich` first."}, True)
            raise typer.Exit(1)
        summary = write_back_to_schematic(project, ref_map, dry_run=dry_run)
        emit(summary, json_out,
             lambda d: console.print(
                 f"[green]{d['symbols_updated']} symbols[/green] in {len(d['files_modified'])} files"
                 f"{' (dry run)' if d['dry_run'] else ''}"))
    except typer.Exit:
        raise
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("review")
def bom_review(
    enriched: Path = typer.Argument(..., help="Enriched BOM CSV (from `dk bom enrich`)"),
    json_out: bool = typer.Option(False, "--json"),
):
    """List rows needing an engineering decision (not_found / needs_review / error).

    For each row shows the query tried, best match, stock, and substitute
    candidates. Confirm a choice with `dk bom pick --ref <REF> --dkpn <PN>`.
    """
    try:
        rows, _prov = load_bom(enriched)
        ignored = [r.get("Reference") or r.get("Refs") for r in rows if (r.get("dk_status") or "") == "ignored"]
        flagged = [r for r in rows if (r.get("dk_status") or "") not in ("found", "ignored", "")]
        out = [
            {
                "reference": r.get("Reference") or r.get("Refs"),
                "value": r.get("Value"),
                "footprint": r.get("Footprint"),
                "status": r.get("dk_status"),
                "reason": r.get("dk_review_reason") or r.get("dk_error") or "",
                "warning": r.get("dk_verify_warning") or "",
                "query": r.get("dk_query") or r.get("dk_tried"),
                "best": {
                    "digikey_pn": r.get("digikey_pn") or r.get("dk_suggested_pn"),
                    "suggested": bool(not (r.get("digikey_pn") or "").strip() and (r.get("dk_suggested_pn") or "").strip()),
                    "mpn": r.get("dk_mpn"),
                    "description": r.get("dk_description"),
                    "stock": r.get("dk_stock"),
                    "price": r.get("dk_unit_price"),
                },
                "alternatives": r.get("dk_alternatives", ""),
            }
            for r in flagged
        ]

        def table(d):
            t = Table(title=f"{len(out)} rows need review")
            for c in ("reference", "value", "status", "reason", "best"):
                t.add_column(c)
            for e in out:
                t.add_row(str(e["reference"]), str(e["value"]), str(e["status"]),
                          str(e["reason"])[:50], str(e["best"]["digikey_pn"]))
            console.print(t)

        emit({"total": len(rows), "need_review": len(out), "ignored": ignored, "rows": out}, json_out, table)
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("pick")
def bom_pick(
    enriched: Path = typer.Argument(..., help="Enriched BOM CSV to update in place"),
    ref: str = typer.Option(..., "--ref", help="Reference (e.g. C19) or grouped refs (C19,C29)"),
    dkpn: str = typer.Option(..., "--dkpn", help="DigiKey part number to assign"),
    json_out: bool = typer.Option(False, "--json"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Confirm an engineering pick: assign a DigiKey PN to BOM row(s) by reference.

    Fetches live details for the part, updates the row's dk_* fields and marks
    it found (if any packaging has stock) or needs_review (if fully OOS).
    The pick is always applied (explicit human decision), but when the part
    does not verify against the row's Value/Footprint/MPN a `warning` is
    returned and stored as `dk_verify_warning` — double-check before ordering.
    The CSV is updated in place (backup saved as <file>.bak).
    """
    from .bom_ops import _row_qty, choose_variant
    from .verify import verify_match
    from .bom_ops import _row_qty, choose_variant

    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, _prov = load_bom(enriched)
        if enriched.suffix.lower() != ".csv":
            emit({"error": "pick only supports CSV BOMs (the enriched output)"}, True)
            raise typer.Exit(1)
        wanted = {r.strip().upper() for r in ref.split(",") if r.strip()}
        details = dk.product_details(s, dkpn)
        prod = details.get("Product", details)
        sp = dk.simplify_product(prod)
        touched: list[str] = []
        warnings: list[str] = []
        for r in rows:
            refs = {x.strip().upper() for x in re.split(r"[,\s]+", (r.get("Reference") or r.get("Refs") or "")) if x.strip()}
            if wanted & refs:
                # Verify against Value/Footprint/MPN (ignoring any stale DKPN
                # columns: this explicit pick overrides them).
                check_row = {**r, "Digikey_PN": "", "DK_PN": "", "digikey_pn": ""}
                ok_v, why_v = verify_match(check_row, sp)
                chosen = choose_variant(sp.get("variations") or [], _row_qty(r))
                stock = (chosen.get("stock") if chosen else sp["quantity_available"]) or 0
                warn = "" if ok_v else f"manual pick does not verify ({why_v})"
                if warn:
                    warnings.append(f"{r.get('Reference', '')}: {warn}")
                r.update(
                    {
                        "digikey_pn": (chosen.get("digikey_pn") if chosen else sp["digikey_pn"]) or dkpn,
                        "dk_pkg": (chosen.get("packaging") if chosen else ""),
                        "dk_moq": (chosen.get("moq") if chosen else ""),
                        "dk_mpn": sp["mpn"],
                        "dk_manufacturer": sp["manufacturer"],
                        "dk_description": sp["description"],
                        "dk_datasheet": sp["datasheet_url"],
                        "dk_product_url": sp["product_url"],
                        "dk_unit_price": str(sp["unit_price"] or ""),
                        "dk_stock": stock,
                        "dk_status": "found" if stock > 0 else "needs_review",
                        "dk_review_reason": "" if stock > 0 else "picked part out_of_stock_everywhere",
                        "dk_verify_warning": warn,
                        "dk_query": f"manual-pick:{dkpn}",
                    }
                )
                touched.append(r.get("Reference", ""))
        if not touched:
            emit({"error": f"No BOM rows match ref(s): {ref}"}, True)
            raise typer.Exit(1)
        backup = enriched.with_suffix(enriched.suffix + ".bak")
        backup.write_bytes(enriched.read_bytes())
        write_csv(rows, enriched)
        emit({"updated_rows": touched, "digikey_pn": dkpn, "stock": stock,
              "warning": "; ".join(warnings),
              "out": str(enriched), "backup": str(backup)}, json_out,
              lambda d: console.print(f"[green]Updated {d['updated_rows']}[/green] → {dkpn} (stock {stock})" +
                                      (f"\n[yellow]WARNING: {d['warning']}[/yellow]" if d["warning"] else "")))
    except typer.Exit:
        raise
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("push-list")
def bom_push_list(
    source: Path = typer.Argument(..., help="Enriched BOM csv, order csv, or project folder"),
    list_name: str = typer.Option(..., "--list-name", "--name", "-n", help="MyLists list name"),
    tags: str = typer.Option("", "--tags", "-t", help="Comma-separated list tags"),
    multiply: int = typer.Option(1, "--multiply", "-m", min=1, help="Boards to build (qty multiplier)"),
    open_browser: bool = typer.Option(False, "--open", help="Open the single-use URL in a browser"),
    include_oos: bool = typer.Option(False, "--include-oos", help="Include out-of-stock/needs-review rows that have a DKPN"),
    json_out: bool = typer.Option(False, "--json"),
    qty_field: str = typer.Option("Qty", "--qty-field"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Create the list on DigiKey: pushes order lines via the MyLists API and
    returns a single-use URL. Open it while signed in to load the BOM into
    your MyLists/cart (nothing is ordered automatically).

    No DigiKey credentials needed for the push itself. Rows still needing an
    engineering pick are skipped and reported.
    """
    import webbrowser

    from .bom_ops import push_thirdparty_list

    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, provenance = load_bom(source)
        if rows and not any(r.get("digikey_pn") or r.get("Digikey_PN") for r in rows):
            console.print("[yellow]No DigiKey PNs found — enriching via API first…[/yellow]")
            rows = enrich_bom(rows, s)
        order, skipped = build_order_list(rows, qty_field=qty_field, include_oos=include_oos)
        if not order:
            emit({"error": "No orderable rows; resolve picks first (`dk bom review`).",
                  "skipped_needs_pick": skipped}, True)
            raise typer.Exit(1)
        url = push_thirdparty_list(order, list_name, tags=tags, multiply=multiply)
        if open_browser:
            webbrowser.open(url)
        emit({"list_name": list_name, "lines": len(order), "multiply": multiply,
              "skipped_needs_pick": skipped, "single_use_url": url}, json_out,
             lambda d: console.print(
                 f"[green]List '{list_name}' ready ({d['lines']} lines).[/green]\n{d['single_use_url']}\n"
                 "Open it while signed into digikey.com to save to MyLists/cart." +
                 (f"\n[yellow]Skipped (need picks): {', '.join(d['skipped_needs_pick'])}[/yellow]" if d["skipped_needs_pick"] else "")))
    except typer.Exit:
        raise
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("exclude")
def bom_exclude(
    enriched: Path = typer.Argument(..., help="Enriched BOM CSV to update in place"),
    ref: list[str] = typer.Option(..., "--ref", help="Reference(s) to exclude; repeatable, commas OK"),
    reason: str = typer.Option("excluded by user", "--reason", "-r"),
    clear: bool = typer.Option(False, "--clear", help="Remove exclusion so the row is processed again"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Exclude generic hardware (pin headers, programming cables…) from ordering.

    Marks matching rows `ignored`: skipped by check-stock/order-list/push-list
    totals and kept across re-enrichment (unless --refresh-ignored).
    CSV is updated in place (backup saved as <file>.bak).
    """
    try:
        rows, _prov = load_bom(enriched)
        if enriched.suffix.lower() != ".csv":
            emit({"error": "exclude only supports CSV BOMs (the enriched output)"}, True)
            raise typer.Exit(1)
        wanted: set[str] = set()
        for chunk in ref:
            wanted.update(r.strip().upper() for r in chunk.split(",") if r.strip())
        touched: list[str] = []
        for r in rows:
            refs = {x.strip().upper() for x in re.split(r"[,\s]+", (r.get("Reference") or r.get("Refs") or "")) if x.strip()}
            if wanted & refs:
                if clear:
                    r["dk_status"] = ""
                    r["dk_review_reason"] = ""
                else:
                    r["dk_status"] = "ignored"
                    r["dk_review_reason"] = reason
                touched.append(r.get("Reference", ""))
        if not touched:
            emit({"error": f"No BOM rows match ref(s): {ref}"}, True)
            raise typer.Exit(1)
        backup = enriched.with_suffix(enriched.suffix + ".bak")
        backup.write_bytes(enriched.read_bytes())
        write_csv(rows, enriched)
        emit({"updated_rows": touched, "ignored": not clear, "out": str(enriched), "backup": str(backup)}, json_out,
             lambda d: console.print(f"[green]Updated {d['updated_rows']}[/green]"))
    except typer.Exit:
        raise
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


def main():
    app()


if __name__ == "__main__":
    main()
