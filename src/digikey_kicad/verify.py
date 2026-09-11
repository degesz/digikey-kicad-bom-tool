"""Conservative part verification — fail closed.

`verify_match(row, product)` decides whether a DigiKey candidate may be
auto-selected for a BOM row. It returns (ok, reason):

- ok=True  -> the pick is defensible (exact MPN, or passive value+package
  both confirmed). Callers may mark the row `found`.
- ok=False -> the pick is NOT defensible. Callers must NOT fill in
  `digikey_pn`; they must flag `needs_review` with the reason and keep the
  candidate only as a suggestion. When in doubt, we alert, never select.

The parsers handle the classic unit traps (`30 mR` vs `30 mΩ` vs `R030`,
`4k7`, `4u7`, metric/imperial package aliases like 0603/1608M).
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# basic normalization
# --------------------------------------------------------------------------

_MFG_ALIASES = {
    # normalized name -> canonical key
    "texasinstruments": "ti",
    "ti": "ti",
    "stmicroelectronics": "st",
    "st": "st",
    "stmicro": "st",
    "samsungelectromechanics": "samsung",
    "samsung": "samsung",
    "samsungem": "samsung",
    "yageo": "yageo",
    "murata": "murata",
    "muratamanufacturing": "murata",
    "tdk": "tdk",
    "vishay": "vishay",
    "vishaydraloric": "vishay",
    "vishaybeyschlag": "vishay",
    "kemet": "kemet",
    "nichicon": "nichicon",
    "panasonic": "panasonic",
    "onsemi": "onsemi",
    "on": "onsemi",
    "nexperia": "nexperia",
    "diodesinc": "diodes",
    "diodesincorporated": "diodes",
    "infineon": "infineon",
    "microchip": "microchip",
    "nxp": "nxp",
    "analogdevices": "adi",
    "adi": "adi",
    "bourns": "bourns",
    "liteon": "liteon",
    "everlight": "everlight",
}


def _alnum_lower(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def normalize_mpn(s: str) -> str:
    """Uppercase, drop spaces/dashes/underscores (stylistic in MPNs)."""
    return re.sub(r"[\s\-_]+", "", (s or "").strip().upper())


def manufacturers_match(row_mfg: str, prod_mfg: str) -> bool:
    """True unless both sides are known and clearly different."""
    a, b = _alnum_lower(row_mfg), _alnum_lower(prod_mfg)
    if not a or not b:
        return True  # can't verify -> don't block on this alone
    if a == b or a in b or b in a:
        return True
    return _MFG_ALIASES.get(a, a) == _MFG_ALIASES.get(b, b)


def strip_pkg_suffix(value: str) -> str:
    """SM712_SOT23 -> SM712 (drop trailing package-like _TOKEN)."""
    toks = (value or "").split("_")
    size = re.compile(
        r"(0201|0402|0603|0805|1206|1210|2010|2512|SOD-?123|SOD-?323|"
        r"SMA|SMB|SMC|SOT-?23|SOT-?223|SOT-?363|SOIC-?8|SOIC-?16|"
        r"TSSOP|QFN|LQFP|QFP|BGA)",
        re.IGNORECASE,
    )
    while len(toks) > 1 and size.fullmatch(toks[-1].upper().replace("-", "")):
        toks.pop()
    out = "_".join(toks)
    return out if out else value


# --------------------------------------------------------------------------
# numeric value parsing (all return SI base units or None)
# --------------------------------------------------------------------------

_R_CODE = re.compile(r"^R(\d{1,4})$", re.IGNORECASE)  # R030 -> 0.030, R10 -> 0.10
_R_MID = re.compile(r"^(\d+)[R](\d+)$", re.IGNORECASE)  # 4R7 -> 4.7
_KMID = re.compile(r"^(\d+)[Kk](\d+)$")  # 4K7 -> 4700
_MMID = re.compile(r"^(\d+)[Mm](\d+)$")  # 4M7 -> 4.7M
_MILLI_OHM = re.compile(
    r"^([\d.,]+)\s*m\s*(?:Ω|Ω|ohms?|R)\b", re.IGNORECASE
)  # 30 mR / 30mΩ
_NUM_UNIT = re.compile(r"^([\d.,]+)\s*([kKmM]?)\s*(Ω|Ω|ohms?|R|F|farads?|H|henr(?:y|ies)?|V(?:olts?)?)?\s*$", re.IGNORECASE)
_CAP = re.compile(r"^([\d.,]+)\s*([pPnNuUµμmM]?)\s*(F|farads?)?\s*(\d*)$", re.IGNORECASE)
_IND = re.compile(r"^([\d.,]+)\s*([pPnNuUµμmM]?)\s*(H|henr(?:y|ies)?)?\s*(\d*)$", re.IGNORECASE)
_FREQ = re.compile(r"^([\d.,]+)\s*([kKmMgG]?)\s*[Hh][Zz]\s*$")
_CURR = re.compile(r"^([\d.,]+)\s*(u|µ|μ|m|A)?\s*[Aa](?:mps?)?\s*$")
_VOLT = re.compile(r"^([\d.,]+)\s*(m|k)?\s*V(?:olts?)?\s*$", re.IGNORECASE)
_VOLT_MID = re.compile(r"^(\d+)[Vv](\d+)$")  # 5V1 -> 5.1
_VOLT_SCAN = re.compile(r"(\d+(?:\.\d+)?)\s*(m|k)?\s*V\b", re.IGNORECASE)


def _f(num: str) -> float | None:
    try:
        return float(num.replace(",", "."))
    except ValueError:
        return None


def parse_resistance(text: str) -> float | None:
    """'10k'->10000, '4k7'->4700, 'R030'->0.03, '30 mR'->0.03. Ohms or None."""
    s = (text or "").strip().replace("µ", "u").replace("μ", "u")
    if not s:
        return None
    m = _R_CODE.match(s)
    if m:
        digits = m.group(1)
        return int(digits) / (10.0 ** len(digits))
    m = _R_MID.match(s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = _KMID.match(s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}") * 1e3
    m = _MMID.match(s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}") * 1e6
    m = _MILLI_OHM.match(s)
    if m:
        v = _f(m.group(1))
        return v / 1000.0 if v is not None else None
    m = _NUM_UNIT.match(s)
    if m and (m.group(3) or m.group(2)):
        v = _f(m.group(1))
        if v is None:
            return None
        mult = {"": 1.0, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}.get(m.group(2), None)
        if mult is None:
            return None
        unit = (m.group(3) or "").upper()
        if unit.startswith("F") or unit.startswith("H") or unit.startswith("V"):
            return None
        return v * mult
    return None


def parse_capacitance(text: str) -> float | None:
    """Farads or None. Requires an explicit prefix or F unit ('100n', '10uF')."""
    s = (text or "").strip().replace("µ", "u").replace("μ", "u")
    m = _CAP.match(s)
    if not m or (not m.group(2) and not m.group(3)):
        return None
    v = _f(m.group(1) + (("." + m.group(4)) if m.group(4) else ""))
    if v is None:
        return None
    mult = {"p": 1e-12, "P": 1e-12, "n": 1e-9, "N": 1e-9, "u": 1e-6,
            "U": 1e-6, "m": 1e-3, "M": 1e-3, "": 1.0}.get(m.group(2), None)
    if mult is None:
        return None
    return v * mult


def parse_inductance(text: str) -> float | None:
    """Henries or None. Requires an explicit prefix or H unit."""
    s = (text or "").strip().replace("µ", "u").replace("μ", "u")
    m = _IND.match(s)
    if not m or (not m.group(2) and not m.group(3)):
        return None
    v = _f(m.group(1) + (("." + m.group(4)) if m.group(4) else ""))
    if v is None:
        return None
    mult = {"p": 1e-12, "P": 1e-12, "n": 1e-9, "N": 1e-9, "u": 1e-6,
            "U": 1e-6, "m": 1e-3, "M": 1e-3, "": 1.0}.get(m.group(2), None)
    if mult is None:
        return None
    return v * mult


def parse_frequency(text: str) -> float | None:
    """Hz or None ('8MHz', '32.768kHz')."""
    m = _FREQ.match((text or "").strip())
    if not m:
        return None
    v = _f(m.group(1))
    if v is None:
        return None
    return v * {"": 1.0, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6,
                "g": 1e9, "G": 1e9}[m.group(2)]


def parse_current(text: str) -> float | None:
    """Amps or None ('2A', '500mA')."""
    m = _CURR.match((text or "").strip().replace("µ", "u").replace("μ", "u"))
    if not m:
        return None
    v = _f(m.group(1))
    if v is None:
        return None
    return v * {"A": 1.0, "m": 1e-3, "u": 1e-6}.get(m.group(2) or "A", 1.0)


def parse_voltage_rating(text: str) -> float | None:
    """Max volts mentioned in text, or None ('50V', '6.3V', '5V1')."""
    s = (text or "").strip()
    m = _VOLT_MID.match(s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    best: float | None = None
    for num, prefix in _VOLT_SCAN.findall(s):
        v = _f(num)
        if v is None:
            continue
        v *= {"m": 1e-3, "k": 1e3}.get((prefix or "").lower(), 1.0)
        best = v if best is None else max(best, v)
    return best


def close_enough(a: float, b: float, tol: float = 0.02) -> bool:
    denom = max(abs(a), abs(b), 1e-18)
    return abs(a - b) <= tol * denom + 1e-12


# --------------------------------------------------------------------------
# package matching
# --------------------------------------------------------------------------

# imperial <-> metric aliases for chip passives (either direction accepted)
_PKG_ALIASES = {
    "0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012",
    "1206": "3216", "1210": "3225", "2010": "5025", "2512": "6332",
    "0603M": "0201", "1005": "0402", "1608": "0603", "2012": "0805",
    "3216": "1206", "3225": "1210", "5025": "2010", "6332": "2512",
}

_PKG_TOKEN_RE = re.compile(
    r"0201|0402|0603|0805|1206|1210|2010|2512|1005|1608|2012|3216|3225|5025|6332|"
    r"SOD-?523|SOD-?723|SOD-?123|SOD-?323|SC-?70|SOT-?523|SOT-?23-?5?|SOT-?223|SOT-?363|"
    r"SMA|SMB|SMC|DO-?214[A-Z]*|SOIC-?\d*|SO-?\d+|TSSOP-?\d*|QFN-?\d*|DFN-?\d*|"
    r"LQFP-?\d*|QFP-?\d*|BGA-?\d*|TO-?220|TO-?252|TO-?263|DPAK|D2PAK|MELF|MELF[A-Z]*",
    re.IGNORECASE,
)


def footprint_tokens(footprint: str) -> list[str]:
    """Normalized package tokens from a KiCad footprint (e.g. 0603)."""
    fp = (footprint or "").upper().replace("_", "-")
    return [re.sub(r"[\s\-_]", "", m.group(0)).upper()
            for m in _PKG_TOKEN_RE.finditer(fp)]


def _blob_nosep(text: str) -> str:
    return re.sub(r"[\s\-_()./]", "", text.upper())


def package_mentioned(token: str, product_text: str) -> bool:
    """Is footprint package `token` (normalized, e.g. 'SOT23') evidenced
    in the DigiKey product text? Handles metric/imperial aliases and
    reversed forms like '8-SOIC'."""
    blob = _blob_nosep(product_text or "")
    tok = re.sub(r"[\s\-_]", "", (token or "").upper())
    if not tok or not blob:
        return False
    if tok in blob:
        return True
    alias = _PKG_ALIASES.get(tok)
    if alias and alias in blob:
        return True
    # reversed pin-count forms: 'SOIC8' <-> '8SOIC', 'SOT23' <-> ... (same)
    m = re.match(r"^([A-Z]+)(\d+)([A-Z]*)$", tok)
    if m and m.group(2):
        rev = m.group(2) + m.group(1) + m.group(3)
        if rev in blob:
            return True
    return False


# --------------------------------------------------------------------------
# row classification + main entry point
# --------------------------------------------------------------------------

def classify_kind(reference: str, value: str) -> str:
    ref = (reference or "").split(",")[0].strip().upper()
    val = (value or "").upper()
    if ref.startswith("D") and "LED" in val:
        return "led"
    if "ZENER" in val:
        return "zener"
    for prefix, kind in (("U", "ic"), ("Y", "crystal"), ("Q", "transistor"),
                         ("D", "diode"), ("R", "resistor"), ("C", "capacitor"),
                         ("L", "inductor"), ("J", "connector"), ("P", "connector"),
                         ("SW", "switch"), ("F", "fuse"), ("K", "relay"),
                         ("T", "transformer"), ("X", "crystal")):
        if ref.startswith(prefix) and ref[len(prefix):len(prefix) + 1].isdigit():
            return kind
    if "LED" in val:
        return "led"
    return "other"


_PARTNO_LIKE = re.compile(r"(?=.*[A-Za-z])(?=.*\d).{3,}")


def product_text(sp: dict) -> str:
    bits = [
        str(sp.get("description") or ""),
        str(sp.get("detailed_description") or ""),
        str(sp.get("category") or ""),
        str(sp.get("packaging") or ""),
        str(sp.get("mpn") or ""),
        str(sp.get("manufacturer") or ""),
    ]
    for v in sp.get("variations") or []:
        bits.append(str(v.get("packaging") or ""))
        bits.append(str(v.get("digikey_pn") or ""))
    for p in sp.get("parameters") or []:
        if isinstance(p, dict):
            bits.append(f"{p.get('name', '')} {p.get('value', '')}")
    return " ".join(bits)


def _param(sp: dict, *needles: str) -> str:
    """First parameter value whose name contains any needle (case-insensitive)."""
    for p in sp.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").lower()
        if any(n in name for n in needles):
            return str(p.get("value") or "")
    return ""


def _row_identifiers(row: dict) -> tuple[str, str, str]:
    dkpn = ""
    for k in ("Digikey_PN", "DK_PN", "digikey_pn"):
        if (row.get(k) or "").strip():
            dkpn = row.get(k).strip()
            break
    mpn = (row.get("MPN") or row.get("mpn") or "").strip()
    mfg = (row.get("Manufacturer") or row.get("manufacturer")
           or row.get("Mfr.") or row.get("MFG") or "").strip()
    return dkpn, mpn, mfg


def verify_match(row: dict, sp: dict) -> tuple[bool, str]:
    """Decide if DigiKey candidate `sp` (simplified product) may be
    auto-selected for BOM `row`. Fail closed: unknown => (False, reason)."""
    ref = (row.get("Reference") or row.get("Refs") or "").strip()
    val = (row.get("Value") or "").strip()
    fp = (row.get("Footprint") or "").strip()
    row_dkpn, row_mpn, row_mfg = _row_identifiers(row)
    prod_mpn = (sp.get("mpn") or "").strip()
    prod_mfg = (sp.get("manufacturer") or "").strip()
    blob = product_text(sp)

    # 1. Explicit DigiKey PN on the row: the candidate must be that exact
    #    part (or one of its packaging variations).
    if row_dkpn:
        want = row_dkpn.strip().upper()
        if (sp.get("digikey_pn") or "").strip().upper() == want:
            return True, "dkpn_exact"
        for v in sp.get("variations") or []:
            if (v.get("digikey_pn") or "").strip().upper() == want:
                return True, "dkpn_variation"
        return False, f"dkpn_mismatch: row wants {row_dkpn}, candidate is {sp.get('digikey_pn')}"

    # 2. Explicit MPN on the row: exact MPN match required (+ mfg sanity).
    if row_mpn:
        if normalize_mpn(strip_pkg_suffix(row_mpn)) != normalize_mpn(prod_mpn):
            return False, f"mpn_mismatch: row MPN {row_mpn} vs candidate {prod_mpn}"
        if not manufacturers_match(row_mfg, prod_mfg):
            return False, f"manufacturer_mismatch: row {row_mfg} vs candidate {prod_mfg}"
        return True, "mpn_exact"

    kind = classify_kind(ref, val)
    bare = strip_pkg_suffix(val).strip()

    # 3. ICs / transistors / diodes-by-part-number: exact MPN required.
    if kind in ("ic", "transistor"):
        if not bare:
            return False, f"{kind}_unverified: empty value, needs exact MPN"
        if normalize_mpn(bare) != normalize_mpn(prod_mpn):
            return False, f"mpn_mismatch: row {bare} vs candidate {prod_mpn} ({kind}s need exact MPN)"
        if not manufacturers_match(row_mfg, prod_mfg):
            return False, f"manufacturer_mismatch: row {row_mfg} vs candidate {prod_mfg}"
        return True, "mpn_exact"

    if kind == "diode":
        if _PARTNO_LIKE.match(bare):
            if normalize_mpn(bare) != normalize_mpn(prod_mpn):
                return False, f"mpn_mismatch: row {bare} vs candidate {prod_mpn} (diodes need exact MPN)"
            return True, "mpn_exact"
        return False, f"diode_unverified: {val!r} is not a resolvable part number"

    if kind == "zener":
        want_v = parse_voltage_rating(val)
        if want_v is None:
            return False, f"zener_unverified: cannot parse voltage from {val!r}"
        if "zener" not in blob.lower():
            return False, f"zener_mismatch: candidate {prod_mpn} is not a zener diode"
        got_v = parse_voltage_rating(_param(sp, "zener")) or parse_voltage_rating(blob)
        if got_v is None:
            return False, "zener_unverified: candidate zener voltage unknown"
        if not close_enough(want_v, got_v):
            return False, f"voltage_mismatch: row {val} vs candidate {got_v}V"
        ok, why = _package_ok(fp, blob)
        return (True, "zener_value+package_ok") if ok else (False, why)

    if kind == "led":
        colors = ("red", "green", "blue", "yellow", "amber", "orange",
                  "white", "warm", "cool", "uv", "infrared", "ir", "rgb")
        want_colors = [c for c in colors if c in val.lower()]
        if want_colors and not any(c in blob.lower() for c in want_colors):
            return False, f"led_mismatch: row {val} vs candidate {sp.get('description')}"
        if "led" not in blob.lower():
            return False, f"led_mismatch: candidate {prod_mpn} is not an LED"
        ok, why = _package_ok(fp, blob)
        return (True, "led_color+package_ok") if ok else (False, why)

    if kind == "crystal":
        want_f = parse_frequency(val) or parse_frequency(val.replace(" ", ""))
        if want_f is None:
            # bare "8M"? try appending Hz markers
            for suffix in ("Hz", "kHz", "MHz"):
                want_f = parse_frequency(bare + suffix)
                if want_f is not None:
                    break
        if want_f is None:
            return False, f"crystal_unverified: cannot parse frequency from {val!r}"
        got_f = parse_frequency(_param(sp, "frequency")) or parse_frequency(blob)
        if got_f is None:
            return False, "crystal_unverified: candidate frequency unknown"
        if not close_enough(want_f, got_f, tol=0.001):
            return False, f"frequency_mismatch: row {val} vs candidate {sp.get('description')}"
        ok, why = _package_ok(fp, blob)
        return (True, "crystal_freq+package_ok") if ok else (False, why)

    # 4. Passives: value AND package must both be confirmed.
    if kind in ("resistor", "capacitor", "inductor"):
        # Values often carry tolerance/voltage suffixes ("100n 50V", "10k 1%"):
        # parse the full token first, then the leading token.
        val_tokens = [bare, bare.split()[0] if bare.split() else bare]

        def _try(parse):
            for tok in dict.fromkeys(val_tokens):
                v = parse(tok)
                if v is not None:
                    return v
            return None

        if kind == "resistor":
            want = _try(parse_resistance)
            got_src = _param(sp, "resistance") or sp.get("description") or ""
            got = parse_resistance(got_src)
            # ferrite beads live under L refs, not here; still, tolerate
            # impedance-style "600R" rows via the same parser.
        elif kind == "capacitor":
            want = _try(parse_capacitance)
            got_src = _param(sp, "capacitance") or sp.get("description") or ""
            got = parse_capacitance(got_src)
        else:
            want = _try(parse_inductance)
            got_src = (_param(sp, "inductance") or sp.get("description") or "")
            got = parse_inductance(got_src)
            if want is None or got is None:
                # ferrite bead ("600R @100MHz"): compare impedance in ohms
                want = _try(parse_resistance)
                got_src = _param(sp, "impedance") or sp.get("description") or ""
                got = parse_resistance(got_src)
        if want is None:
            return False, f"value_unverified: cannot parse {kind} value from {val!r}"
        if got is None:
            return False, f"value_unverified: candidate value unknown ({(got_src or '')[:60]})"
        if not close_enough(want, got):
            return False, f"value_mismatch: row {val} vs candidate {(got_src or '').strip()[:60]}"
        ok, why = _package_ok(fp, blob)
        if not ok:
            return False, why
        # voltage rating: a stated requirement must be met by the part
        want_v = parse_voltage_rating(val)
        if want_v is not None:
            got_v = (parse_voltage_rating(_param(sp, "voltage", "rated"))
                     or parse_voltage_rating(blob))
            if got_v is None:
                return False, "voltage_unverified: row states a voltage, candidate rating unknown"
            if got_v < want_v * 0.999:
                return False, f"voltage_rating_insufficient: need >= {want_v}V, candidate {got_v}V"
        return True, f"{kind}_value+package_ok"

    # 5. Everything else (connectors, switches, fuses, ...): keyword hits
    #    routinely return the wrong mating/mounting variant — demand an MPN.
    label = kind if kind != "other" else "part"
    return False, f"needs_exact_mpn: {label} {ref} {val!r} has no MPN; keyword picks are unreliable"


def _package_ok(fp: str, blob: str) -> tuple[bool, str]:
    toks = footprint_tokens(fp)
    if not toks:
        return False, f"package_unverified: no recognizable size in footprint {fp!r}"
    if "wide" in (fp or "").lower() and "wide" not in (blob or "").lower():
        return False, f"package_mismatch: footprint {fp} is wide-body, candidate is standard"
    missing = [t for t in toks if not package_mentioned(t, blob)]
    if missing:
        return False, f"package_mismatch: footprint {fp} ({'/'.join(toks)}) not evidenced in candidate"
    return True, "package_ok"
