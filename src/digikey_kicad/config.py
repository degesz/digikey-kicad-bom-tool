"""Config resolution: env/flags/file only — no committed secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# No built-in credentials — set via env vars, CLI flags, or .env (see .env.example).
# Never commit real client secrets to git.
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""
DEFAULT_ENV = "production"  # or "sandbox"
DEFAULT_SITE = "US"
DEFAULT_LANG = "en"
DEFAULT_CURRENCY = "USD"

PROD_BASE = "https://api.digikey.com"
SANDBOX_BASE = "https://sandbox-api.digikey.com"


@dataclass
class Settings:
    client_id: str = DEFAULT_CLIENT_ID
    client_secret: str = DEFAULT_CLIENT_SECRET
    env: str = DEFAULT_ENV
    site: str = DEFAULT_SITE
    lang: str = DEFAULT_LANG
    currency: str = DEFAULT_CURRENCY

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE if self.env == "sandbox" else PROD_BASE

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/v1/oauth2/token"

    @property
    def keyword_url(self) -> str:
        return f"{self.base_url}/products/v4/search/keyword"

    def product_details_url(self, part: str) -> str:
        from urllib.parse import quote

        return f"{self.base_url}/products/v4/search/{quote(part, safe='')}/productdetails"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_settings(
    client_id: str | None = None,
    client_secret: str | None = None,
    env: str | None = None,
    site: str | None = None,
    lang: str | None = None,
    currency: str | None = None,
) -> Settings:
    """Precedence: CLI flags > real env vars > .env (cwd / ~/.config/digikey-kicad/.env) > empty."""
    dotenv = _load_dotenv(Path.cwd() / ".env")
    # also support ~/.config/digikey-kicad/.env
    cfg_dotenv = _load_dotenv(Path.home() / ".config" / "digikey-kicad" / ".env")
    merged = {**cfg_dotenv, **dotenv}

    def pick(flag: str | None, env_key: str, default: str) -> str:
        if flag:
            return flag
        if os.getenv(env_key):
            return os.getenv(env_key, default)
        return merged.get(env_key, default)

    s = Settings(
        client_id=pick(client_id, "DIGIKEY_CLIENT_ID", DEFAULT_CLIENT_ID),
        client_secret=pick(client_secret, "DIGIKEY_CLIENT_SECRET", DEFAULT_CLIENT_SECRET),
        env=pick(env, "DIGIKEY_ENV", DEFAULT_ENV),
        site=pick(site, "DIGIKEY_SITE", DEFAULT_SITE),
        lang=pick(lang, "DIGIKEY_LANG", DEFAULT_LANG),
        currency=pick(currency, "DIGIKEY_CURRENCY", DEFAULT_CURRENCY),
    )
    if s.env not in ("production", "sandbox"):
        s.env = "production"
    return s


def require_credentials(s: Settings) -> Settings:
    """Fail fast with a helpful message when API credentials are missing."""
    if not s.client_id or not s.client_secret:
        raise RuntimeError(
            "Missing DigiKey API credentials. Set DIGIKEY_CLIENT_ID and "
            "DIGIKEY_CLIENT_SECRET env vars (or --client-id/--client-secret flags, "
            "or a local .env — see .env.example). Get keys at developer.digikey.com."
        )
    return s
