"""KiCad project -> BOM resolution.

Supports pointing at a KiCad 10 project *folder*:
  1. If <project>/*.csv BOM already exists, use the newest one.
  2. Else if kicad-cli is installed, run `sch export bom` (CSV) or
     `sch export python-bom` (XML) against the root .kicad_sch.
  3. Else parse the .kicad_sch S-expression directly (Value/Footprint/MPN fields).

Also parses standalone .csv / .xml BOM files.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def find_schematics(project_dir: Path) -> list[Path]:
    seen: dict[str, Path] = {}
    for pat in ("*.kicad_sch", "**/*.kicad_sch"):
        for p in project_dir.glob(pat):
            # skip KiCad backups / history
            if "-backups" in str(p) or ".history" in str(p):
                continue
            seen[str(p.resolve())] = p
    return sorted(seen.values(), key=lambda p: p.name)


def find_root_sch(project_dir: Path, schs: list[Path] | None = None) -> Path | None:
    schs = schs if schs is not None else find_schematics(project_dir)
    if not schs:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[\s_\-]+", "", s).lower()

    # Prefer file matching the project/folder name (KiCad convention;
    # tolerate space/underscore/dash differences, e.g. "power card" vs power_card)
    nstem = norm(project_dir.name)
    for p in schs:
        if norm(p.stem) == nstem and p.parent == project_dir:
            return p
    # Else the top-level .kicad_sch that instantiates sheets (hierarchical root)
    for p in schs:
        if p.parent != project_dir:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if re.search(r"(?m)^\t\(sheet\b", txt):
            return p
    # Fallback: largest top-level sheet
    top = [p for p in schs if p.parent == project_dir] or schs
    return max(top, key=lambda p: p.stat().st_size if p.exists() else 0)


def find_existing_boms(project_dir: Path) -> list[Path]:
    cands = list(project_dir.glob("*bom*.csv")) + list(project_dir.glob("*bom*.xml"))
    cands += list(project_dir.glob("*.csv")) + list(project_dir.glob("*.xml"))
    # newest first
    cands = [c for c in cands if c.is_file()]
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands


def kicad_cli() -> str | None:
    return shutil.which("kicad-cli")


def export_bom_via_kicad_cli(sch: Path, out: Path) -> Path:
    cli = kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found on PATH")
    # Preferred: modern CSV export
    cmd = [
        cli, "sch", "export", "bom",
        "--fields", "Reference,Value,Footprint,${QUANTITY},${DNP},MPN,Manufacturer,Digikey_PN",
        "-o", str(out), str(sch),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out.exists():
        # Fallback: legacy python-bom XML
        xml_out = out.with_suffix(".xml")
        cmd2 = [cli, "sch", "export", "python-bom", "-o", str(xml_out), str(sch)]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=120)
        if r2.returncode != 0 or not xml_out.exists():
            raise RuntimeError(f"kicad-cli BOM export failed: {r.stderr[:500]} | {r2.stderr[:500]}")
        return xml_out
    return out


def parse_csv_bom(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def parse_xml_bom(path: Path) -> list[dict]:
    """Parse KiCad python-bom legacy XML (components/comp)."""
    tree = ET.parse(path)
    root = tree.getroot()
    rows: list[dict] = []
    for comp in root.findall(".//comp"):
        row: dict[str, str] = {"Reference": comp.get("ref", "")}
        val = comp.find("value")
        fp = comp.find("footprint")
        if val is not None and val.text:
            row["Value"] = val.text.strip()
        if fp is not None and fp.text:
            row["Footprint"] = fp.text.strip()
        fields = comp.find("fields")
        if fields is not None:
            for field in fields.findall("field"):
                row[field.get("name", "field")] = (field.text or "").strip()
        lib = comp.find("libsource")
        if lib is not None:
            row.setdefault("LibPart", (lib.get("part", "") or ""))
        rows.append(row)
    return rows


def _placed_symbol_blocks(text: str) -> list[str]:
    """Split a .kicad_sch into top-level placed `(symbol` blocks.

    Placed symbols start at exactly one-tab indent (`\\t(symbol`), while
    library definitions and glyphs are indented deeper.
    """
    pat = re.compile(r"(?m)^\t\(symbol\n.*?(?=^\t\(|\Z)", re.DOTALL)
    blocks = pat.findall(text)
    if not blocks:  # fallback for atypical formatting
        blocks = re.findall(r"\(symbol\b.*?\(pin\b.*?\)\s*\)", text, re.DOTALL)
    return blocks[:5000]


def parse_sch_entries(sch: Path) -> list[dict]:
    """Parse one .kicad_sch into per-symbol entries (ungrouped).

    Each entry carries _source_file for schematic write-back.
    """
    text = sch.read_text(encoding="utf-8", errors="ignore")
    entries: list[dict] = []
    for sym in _placed_symbol_blocks(text):
        if '(property "Reference"' not in sym:
            continue
        props = dict(re.findall(r'\(property\s+"([^"]+)"\s+"([^"]*)"', sym))
        ref = props.get("Reference", "")
        if not ref or ref.startswith("#"):
            continue  # power symbols, etc.
        dnp = "(dnp yes)" in sym
        mpn = (
            props.get("Manufacturer Part")
            or props.get("MPN", "")
            or props.get("Mfr. No.", "")
        )
        mfg = props.get("Manufacturer") or props.get("Mfr.", "")
        entries.append(
            {
                "Reference": ref,
                "Value": props.get("Value", ""),
                "Footprint": props.get("Footprint", ""),
                "MPN": mpn,
                "Manufacturer": mfg,
                "LCSC": props.get("LCSC", ""),
                "Digikey_PN": props.get("Digikey_PN", props.get("DK_PN", props.get("DigiKey_PN", ""))),
                "Datasheet": props.get("Datasheet", ""),
                "Description": props.get("Description", ""),
                "DNP": "yes" if dnp else "",
                "_source_file": str(sch),
            }
        )
    return entries


def parse_sch_direct(sch: Path) -> list[dict]:
    """Lightweight .kicad_sch parse: group symbols by Value+Footprint.

    Extracts property "Value", "Footprint", plus MPN/Manufacturer/Digikey_PN if present.
    """
    entries = parse_sch_entries(sch)
    return group_entries(entries)


def parse_project_schs(project_dir: Path) -> tuple[list[dict], list[dict]]:
    """Parse ALL schematic sheets in a project. Returns (grouped_rows, entries)."""
    schs = find_schematics(project_dir)
    # only sheets reachable from root ideally, but KiCad globs are fine;
    # restrict to top-level dir sheets (skip subfolders except explicit)
    entries: list[dict] = []
    per_file: dict[str, int] = {}
    for sch in schs:
        es = parse_sch_entries(sch)
        per_file[sch.name] = len(es)
        entries.extend(es)
    rows = group_entries(entries)
    return rows, entries


def group_entries(entries: list[dict]) -> list[dict]:
    # Group identical Value+Footprint+MPN into one row with combined refs
    grouped: dict[tuple, dict] = {}
    for e in entries:
        key = (e["Value"], e["Footprint"], e.get("MPN", ""), e.get("Digikey_PN", ""))
        g = grouped.setdefault(key, {**e, "Reference": [], "Qty": 0})
        if isinstance(g["Reference"], list):
            g["Reference"].append(e["Reference"])
        g["Qty"] += 1
    rows = []
    for g in grouped.values():
        refs = g["Reference"] if isinstance(g["Reference"], list) else [g["Reference"]]
        row = {k: v for k, v in g.items() if not k.startswith("_")}
        row.update({"Reference": ",".join(sorted(refs)), "Refs": ",".join(sorted(refs))})
        rows.append(row)
    rows.sort(key=lambda r: r["Reference"])
    return rows


def expand_references(rows: list[dict]) -> dict[str, dict]:
    """Expand grouped BOM rows (Reference='R1,R2') into per-ref dict."""
    out: dict[str, dict] = {}
    for r in rows:
        refs = re.split(r"[,\s]+", (r.get("Reference") or r.get("Refs") or "").strip())
        for ref in refs:
            if ref:
                out[ref.strip()] = r
    return out


def load_bom(source: Path) -> tuple[list[dict], str]:
    """Load a BOM from a file (.csv/.xml) or a project dir. Returns (rows, provenance)."""
    if source.is_file():
        if source.suffix.lower() == ".csv":
            return parse_csv_bom(source), f"csv:{source}"
        if source.suffix.lower() == ".xml":
            return parse_xml_bom(source), f"xml:{source}"
        if source.suffix.lower() in (".kicad_sch",):
            rows = parse_sch_direct(source)
            return rows, f"sch-direct:{source}"
        raise ValueError(f"Unsupported BOM file type: {source.suffix}")
    if source.is_dir():
        schs = find_schematics(source)
        if not schs:
            raise FileNotFoundError(f"No .kicad_sch or BOM found in {source}")
        root = find_root_sch(source, schs) or schs[0]
        if kicad_cli():
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
                out = Path(tf.name)
            try:
                got = export_bom_via_kicad_cli(root, out)
                rows, _ = load_bom(got)
                return rows, f"kicad-cli:{root}->{got}"
            finally:
                pass
        rows, _entries = parse_project_schs(source)
        return rows, f"sch-direct:{root}+{len(schs) - 1} sheets"
    raise FileNotFoundError(f"BOM source not found: {source}")


REF_KIND_HINTS = [
    ("R", "resistor"),
    ("C", "capacitor"),
    ("L", "inductor"),
    ("D", "diode"),
    ("Q", "transistor"),
    ("Y", "crystal"),
    ("J", "connector"),
    ("SW", "switch"),
    ("F", "fuse"),
]

SIZE_RE = re.compile(
    r"(0201|0402|0603|0805|1206|1210|2010|2512|"
    r"SOD-?123|SOD-?323|SMA|SMB|SMC|SOT-?23|SOT-?223|SOT-?363|"
    r"SOIC-?8|SOIC-?16|TSSOP|QFN|LQFP|QFP|BGA|SMPM?)",
    re.IGNORECASE,
)


def _package_hint(footprint: str) -> str:
    m = SIZE_RE.search((footprint or "").upper().replace("_", "-"))
    return m.group(1).upper() if m else ""


def _kind_hint(reference: str, value: str) -> str:
    ref = (reference or "").split(",")[0].strip().upper()
    val = (value or "").upper()
    if ref.startswith("D") and "LED" in val:
        return "LED"
    if "ZENER" in val:
        return "zener diode"
    # longest-prefix match, e.g. SW100 -> SW (not S), R37 -> R
    for prefix, hint in sorted(REF_KIND_HINTS, key=lambda x: -len(x[0])):
        if ref.startswith(prefix) and ref[len(prefix):len(prefix) + 1].isdigit():
            return hint
    return ""


def build_search_query(row: dict) -> str:
    """Build the best DigiKey keyword query for a BOM row.

    Prefers exact identifiers; falls back to `Value + package + kind`
    (never the raw `Library:Footprint` string, which poisons results).
    """
    for key in ("Digikey_PN", "DK_PN", "MPN", "mpn"):
        v = (row.get(key) or "").strip()
        if v:
            return v
    val = (row.get("Value") or "").strip()
    if not val:
        return (row.get("Description") or "").strip()
    # ICs / specific parts: Value already is the part number
    ref = (row.get("Reference") or row.get("Refs") or "").split(",")[0].strip().upper()
    if ref.startswith("U") or ref.startswith("Y") or re.match(r"^[A-Z]+\d+$", val):
        return val
    parts = [val]
    pkg = _package_hint(row.get("Footprint") or "")
    if pkg:
        parts.append(pkg)
    kind = _kind_hint(row.get("Reference") or row.get("Refs") or "", val)
    if kind:
        parts.append(kind)
    return " ".join(parts)


def row_search_text(row: dict) -> str:
    return build_search_query(row)


PROPERTY_TEMPLATE = (
    '\t\t(property "{name}" "{value}"\n'
    '\t\t\t(at 0 0 0)\n'
    '\t\t\t(show_name no)\n'
    '\t\t\t(do_not_autoplace no)\n'
    '\t\t\t(hide yes)\n'
    '\t\t\t(effects\n'
    '\t\t\t\t(font\n'
    '\t\t\t\t\t(size 1.27 1.27)\n'
    '\t\t\t\t)\n'
    '\t\t\t)\n'
    '\t\t)'
)


def _set_property_in_symbol(sym_text: str, name: str, value: str) -> tuple[str, bool]:
    """Set (property "name" "value") inside one top-level symbol block.

    Returns (new_text, changed). Escapes quotes/backslashes in value.
    """
    safe = value.replace("\\", "\\\\").replace('"', '\\"')
    pat = re.compile(r'\(property\s+"%s"\s+"(?:[^"\\]|\\.)*"' % re.escape(name))
    m = pat.search(sym_text)
    if m:
        new_prop = f'(property "{name}" "{safe}"'
        new_text = sym_text[: m.start()] + new_prop + sym_text[m.end():]
        return new_text, new_text != sym_text
    # Insert after the last property block: anchor on first non-property
    # element inside a placed symbol (nested glyph, pin list, instances...).
    anchor = re.search(r"(?m)^\t\t\(symbol\s+|^[\t ]*\(pin\s+\"|\(instances\b|\(embedded_fonts\b", sym_text)
    if anchor:
        insert_at = anchor.start()
    else:
        insert_at = len(sym_text.rstrip()) - 1  # before final closing paren
    insertion = PROPERTY_TEMPLATE.format(name=name, value=safe) + "\n"
    new_text = sym_text[:insert_at] + insertion + sym_text[insert_at:]
    return new_text, True


def write_back_to_schematic(
    project_dir: Path,
    ref_map: dict[str, dict],
    dry_run: bool = False,
) -> dict:
    """Write Digikey_PN + Datasheet fields into .kicad_sch symbols.

    ref_map: Reference -> {"digikey_pn": ..., "datasheet": ...}.
    Returns summary dict {files_modified, symbols_updated, details}.
    """
    schs = find_schematics(project_dir)
    files_modified: list[str] = []
    details: list[dict] = []
    symbols_updated = 0
    for sch in schs:
        text = sch.read_text(encoding="utf-8", errors="ignore")
        # split into top-level placed symbol spans with offsets
        spans: list[tuple[int, int, str, str]] = []  # start, end, ref, block
        pattern = re.compile(r"(?m)^\t\(symbol\n.*?(?=^\t\(|\Z)", re.DOTALL)
        matches = list(pattern.finditer(text))
        if not matches:  # fallback for atypical formatting
            pattern = re.compile(r"\(symbol\b.*?\(pin\b.*?\)\s*\)", re.DOTALL)
            matches = list(pattern.finditer(text))
        for m in matches:
            block = m.group(0)
            if '(property "Reference"' not in block:
                continue
            rm = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', block)
            if not rm:
                continue
            ref = rm.group(1)
            if not ref or ref.startswith("#"):
                continue
            spans.append((m.start(), m.end(), ref, block))
        if not spans:
            continue
        new_text = text
        offset = 0
        file_updates = 0
        for start, end, ref, block in spans:
            info = ref_map.get(ref)
            if not info:
                continue
            dkpn = str(info.get("digikey_pn") or "").strip()
            ds = str(info.get("datasheet") or info.get("dk_datasheet") or "").strip()
            if not dkpn and not ds:
                continue
            s, e = start + offset, end + offset
            cur = new_text[s:e]
            changed_any = False
            if dkpn:
                cur, ch = _set_property_in_symbol(cur, "Digikey_PN", dkpn)
                changed_any = changed_any or ch
            if ds:
                cur, ch = _set_property_in_symbol(cur, "Datasheet", ds)
                changed_any = changed_any or ch
            if changed_any:
                new_text = new_text[:s] + cur + new_text[e:]
                offset += len(cur) - (e - s)
                symbols_updated += 1
                file_updates += 1
                details.append({"ref": ref, "file": sch.name, "digikey_pn": dkpn})
        if file_updates:
            files_modified.append(sch.name)
            if not dry_run:
                backup = sch.with_suffix(sch.suffix + ".dkbak")
                backup.write_text(text, encoding="utf-8")
                sch.write_text(new_text, encoding="utf-8")
    return {
        "files_modified": files_modified,
        "symbols_updated": symbols_updated,
        "details": details,
        "dry_run": dry_run,
    }
