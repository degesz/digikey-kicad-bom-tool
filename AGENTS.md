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
dk bom push-list <enriched.csv|order.csv|project-dir> --list-name NAME [--tags T] [--multiply N] [--open] --json
dk bom review <enriched.csv> --json
dk bom pick <enriched.csv> --ref <REF> --dkpn <DKPN> --json
dk bom exclude <enriched.csv> --ref <REF...> [--reason R] [--clear] --json
dk bom write-back <enriched.csv> <project-dir> [--dry-run] --json
```

## Typical flows

1. Complete a KiCad BOM:
   `dk bom from-project ./proj --json` → `dk bom enrich ./proj --out enriched.csv --json`
2. Stock check (packaging-agnostic, cut tape accepted — `ok` means ANY variant in stock):
   `dk bom check-stock ./enriched.csv --json`
3. Review engineering picks: `dk bom review ./enriched.csv --json`, confirm with
   `dk bom pick ./enriched.csv --ref C19 --dkpn <DKPN> --json`, re-run check-stock.
4. Order list: `dk bom order-list ./enriched.csv --out order.csv --json` then tell the user to upload at digikey.com → MyLists → Upload BOM.
   Or create it directly: `dk bom push-list ./enriched.csv --list-name NAME --json` → user opens
   the single-use URL while signed in to save it to MyLists/cart (no credentials needed).
5. Write back into schematics (`Digikey_PN` + `Datasheet` + `Digikey_URL` per symbol,
   backups in `<proj>/.dk-backups/`, loose `*.dkbak` auto-migrated there):
   `dk bom write-back ./enriched.csv ./proj --json`
6. Single part: `dk search "STM32G431KBT6" --json` → `dk details <DKPN> --json`.

## Conventions

- Errors: exit ≠ 0, stdout is `{"error": "..."}` — surface the message.
- Enriched rows carry: `digikey_pn, dk_pkg, dk_moq, dk_mpn, dk_manufacturer,
  dk_description, dk_datasheet, dk_product_url, dk_stock, dk_unit_price,
  dk_status (found|needs_review|not_found|error), dk_review_reason, dk_alternatives`.
- Enrich policy: exact MPN wins; generic passives ranked by stock then price;
  chosen variant prefers in-stock cut tape / Digi-Reel (lowest MOQ covering Qty).
  Marketplace variations are never chosen (flagged `marketplace_only`).
  `needs_review` rows need a human/agent pick via `dk bom pick`.
- Generic hardware (cables, pin headers) can be dropped from ordering with
  `dk bom exclude` (status `ignored`, kept across re-enrichment).
- Credentials: flags `--client-id/--client-secret/--env` override env `DIGIKEY_CLIENT_ID/...`; do not print secrets.
- Never commit `.env` or token cache (`~/.cache/digikey-kicad/`).
