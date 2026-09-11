# digikey-kicad-bom-tool

DigiKey API + KiCad BOM command-line tool, designed for **AI agents** (`--json` everywhere).

Main uses:
- **Search** DigiKey components (`dk search`, `dk details`, `dk stock`)
- **Complete KiCad BOMs** — point at a KiCad 10 project folder; the tool uses `kicad-cli` if present, else parses `.kicad_sch` directly (`dk bom from-project`, `dk bom enrich` adds DigiKey PN + datasheet + description + stock + price)
- **Stock check** for BOM rows (`dk bom check-stock`)
- **Order lists** as DigiKey-uploadable CSV for MyLists / BOM Manager (`dk bom order-list`)

## Install

```bash
pip install -e .
# or: pipx install -e .
dk --help
```

Requires Python ≥ 3.10. Optional: `kicad-cli` on PATH for best BOM export.

## Credentials (never committed)

No credentials are shipped with this tool. Provide your own DigiKey
`client_credentials` app keys (https://developer.digikey.com/).
Override precedence (highest first):

1. CLI flags: `--client-id`, `--client-secret`, `--env`
2. Real env vars: `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `DIGIKEY_ENV`, `DIGIKEY_SITE`, `DIGIKEY_LANG`, `DIGIKEY_CURRENCY`
3. `.env` in cwd, or `~/.config/digikey-kicad/.env`
4. Empty (commands fail with a helpful message until credentials are set)

```bash
cp .env.example .env   # then edit
export DIGIKEY_CLIENT_ID=... DIGIKEY_CLIENT_SECRET=...
dk auth --json
dk config-show
```

Default API env is **production** (`https://api.digikey.com`); use `--env sandbox` for testing.

> Never commit `.env` or real secrets. If a key ever leaks, revoke/rotate it
> immediately in the DigiKey portal — removing it from git history is not enough.

## Usage

```bash
# auth check
dk auth
# search (human table; add --json for agents)
dk search "10k 0603 resistor" --limit 5 --json
dk search "STM32G431" --in-stock-only --json
dk details 311-1.0KFRCT-ND --json
dk stock 311-1.0KFRCT-ND ATMEGA328P-PU-ND --json

# KiCad: point at project folder (or a .csv/.xml/.kicad_sch directly)
dk bom from-project ./my-kicad-project --json
dk bom from-project ./my-kicad-project --out bom.csv

# enrich BOM with DKPN + datasheet + description + stock + price
dk bom enrich ./my-kicad-project --out enriched_bom.csv --json
dk bom enrich ./bom.csv --out enriched_bom.csv

# stock check
dk bom check-stock ./enriched_bom.csv --json --out stock.csv

# order list (DigiKey MyLists-compatible CSV)
dk bom order-list ./enriched_bom.csv --out order_list.csv --json
# then: digikey.com → MyLists → Create List → Upload BOM/CSV
```

Every command supports `--json` for deterministic agent parsing. Exit code ≠ 0 on error with `{"error": ...}` on stdout.

## MyLists / Cart note

DigiKey cart & MyLists **writes** require 3-legged user OAuth (browser login), which this CLI intentionally does not perform.
`dk bom order-list` produces a CSV directly importable via the DigiKey website (MyLists → Upload). This keeps the tool usable with 2-legged `client_credentials` tokens.

## Layout

```
src/digikey_kicad/
  cli.py         Typer commands (`dk ...`)
  config.py      defaults + env + .env resolution
  digikey_api.py OAuth token cache + Product Info v4 (keyword/details)
  kicad_bom.py   project-folder → BOM (kicad-cli / xml / csv / .sch direct)
  bom_ops.py     enrich / stock / order-list CSV
AGENTS.md        instructions for AI agents
examples/        sample BOM
```

## DigiKey API reference

- Token: `POST {base}/v1/oauth2/token` (`client_id`, `client_secret`, `grant_type=client_credentials`)
- Keyword: `POST {base}/products/v4/search/keyword` `{"Keywords","Limit","Offset"}`
- Details: `GET {base}/products/v4/search/{part}/productdetails`
- Headers: `X-DIGIKEY-Client-Id`, `Authorization: Bearer …`, `X-DIGIKEY-Locale-{Site,Language,Currency}`, `X-DIGIKEY-Customer-Id: 0`
