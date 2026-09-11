"""`dk` command interface — human-friendly tables, `--json` for AI agents."""
from __future__ import annotations

import json
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
    json_out: bool = typer.Option(False, "--json"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Match each BOM row to DigiKey: adds DKPN + datasheet + description + stock + price."""
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, provenance = load_bom(source)
        enriched = enrich_bom(rows, s, limit=min(max(limit, 1), 10))
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
        ok = sum(1 for r in results if r.get("ok"))

        def table(d):
            t = Table(title=f"Stock: {ok}/{len(results)} in stock")
            for c in ("Reference", "digikey_pn", "stock", "unit_price"):
                t.add_column(c)
            for r in results:
                t.add_row(str(r.get("Reference", "")), str(r.get("digikey_pn", "")), str(r.get("stock", "")), str(r.get("unit_price", "")))
            console.print(t)

        emit({"provenance": provenance, "in_stock": ok, "total": len(results), "rows": results,
              "out": str(out) if out else None}, json_out, table)
    except Exception as e:
        emit({"error": str(e)}, True)
        raise typer.Exit(1)


@bom_app.command("order-list")
def bom_order_list(
    source: Path = typer.Argument(..., help="Enriched BOM csv or project folder"),
    out: Path = typer.Option(Path("order_list.csv"), "--out", "-o"),
    json_out: bool = typer.Option(False, "--json"),
    qty_field: str = typer.Option("Qty", "--qty-field"),
    client_id: Optional[str] = typer.Option(None, "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    env: Optional[str] = typer.Option(None, "--env"),
):
    """Build a DigiKey-compatible order list CSV (upload to MyLists/BOM Manager).

    Note: DigiKey cart/MyLists writes require 3-legged user OAuth, which this
    CLI does not perform. The CSV produced here is directly importable at
    digikey.com → MyLists → Create List → Upload BOM/CSV.
    """
    s = _settings(client_id, client_secret, env, None, None, None)
    try:
        rows, provenance = load_bom(source)
        # auto-enrich if rows lack digikey_pn
        if rows and not any(r.get("digikey_pn") or r.get("Digikey_PN") for r in rows):
            console.print("[yellow]No DigiKey PNs found — enriching via API first…[/yellow]", file=sys.stderr)
            rows = enrich_bom(rows, s)
        order = build_order_list(rows, qty_field=qty_field)
        if not order:
            emit({"error": "No rows with DigiKey part numbers; enrich the BOM first.", "provenance": provenance}, True)
            raise typer.Exit(1)
        write_csv(order, out)
        emit({"provenance": provenance, "lines": len(order), "out": str(out), "order": order}, json_out,
             lambda d: console.print(f"[green]{d['lines']} order lines → {d['out']}[/green]\nUpload at digikey.com → MyLists → Upload BOM."))
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
    """Write Digikey_PN + Datasheet fields back into the .kicad_sch symbols.

    Matches enriched rows to schematic symbols by Reference. Originals are
    backed up as *.kicad_sch.dkbak before modification.
    """
    from .kicad_bom import expand_references, write_back_to_schematic

    try:
        rows, _prov = load_bom(enriched)
        ref_map: dict[str, dict] = {}
        for ref, r in expand_references(rows).items():
            dkpn = (r.get("digikey_pn") or r.get("Digikey_PN") or "").strip()
            ds = (r.get("dk_datasheet") or r.get("Datasheet") or r.get("datasheet") or "").strip()
            if dkpn or ds:
                ref_map[ref] = {"digikey_pn": dkpn, "datasheet": ds}
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


def main():
    app()


if __name__ == "__main__":
    main()
