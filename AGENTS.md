# AGENTS.md — instructions for AI agents using this repo

This tool is a CLI named `dk`. Prefer `--json` for all calls; parse stdout as JSON.

## Commands

```bash
dk auth --json
dk config-show --json
dk search "<keywords>" --limit 10 --json [--in-stock-only]
dk details "<DKPN>" --json
dk stock <DKPN...> --json
dk bom from-project <project-dir|bom.csv|bom.xml|file.kicad_sch> [--out bom.csv] --json
dk bom enrich <project-dir|bom.csv> --out enriched_bom.csv --json
dk bom check-stock <enriched.csv|project-dir> [--out stock.csv] --json
dk bom order-list <enriched.csv|project-dir> --out order_list.csv --json
```

## Typical flows

1. Complete a KiCad BOM:
   `dk bom from-project ./proj --json` → `dk bom enrich ./proj --out enriched.csv --json`
2. Stock check: `dk bom check-stock ./enriched.csv --json`
3. Order list: `dk bom order-list ./enriched.csv --out order.csv --json` then tell the user to upload at digikey.com → MyLists → Upload BOM.
4. Single part: `dk search "STM32G431KBT6" --json` → `dk details <DKPN> --json`.

## Conventions

- Errors: exit ≠ 0, stdout is `{"error": "..."}` — surface the message.
- Enriched rows carry: `digikey_pn, dk_mpn, dk_manufacturer, dk_description, dk_datasheet, dk_product_url, dk_stock, dk_unit_price, dk_status`.
- Credentials: flags `--client-id/--client-secret/--env` override env `DIGIKEY_CLIENT_ID/...`; do not print secrets.
- Never commit `.env` or token cache (`~/.cache/digikey-kicad/`).
