"""Input validation."""
from __future__ import annotations
import io, csv, re
from config import Config

CUI_RE = re.compile(r"^\d{1,10}$")


def validate_cui(raw) -> tuple[bool, str, int | None]:
    if raw is None or str(raw).strip() == "":
        return False, "CUI is empty", None
    s = str(raw).strip().upper().lstrip("RO").strip()
    if not CUI_RE.match(s):
        return False, f"CUI must be 1–10 digits (got: {raw})", None
    v = int(s)
    if v <= 0:
        return False, f"CUI must be positive", None
    return True, "", v


def validate_years(years: list[int]) -> tuple[bool, str]:
    if not years:
        return False, "Select at least one year"
    for y in years:
        if not isinstance(y, int) or y < Config.FINANCIALS_MIN_YEAR or y > Config.FINANCIALS_MAX_YEAR:
            return False, f"Year {y} out of range ({Config.FINANCIALS_MIN_YEAR}–{Config.FINANCIALS_MAX_YEAR})"
    if len(years) > 11:
        return False, "Maximum 11 years"
    return True, ""


def validate_and_parse_csv(content: bytes) -> tuple[bool, str, list[int], list[str]]:
    if not content:
        return False, "File is empty", [], []
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return False, "Cannot decode file (use UTF-8)", [], []

    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    except Exception as ex:
        return False, f"CSV parse error: {ex}", [], []

    cui_col = next((h for h in headers if h in ("cui", "cif", "cod_fiscal", "fiscal_code", "vat", "tax_id")), None)
    if cui_col is None:
        return False, f"No CUI column found. Headers: {', '.join(headers) or '(none)'}", [], []

    valid: list[int] = []
    warnings: list[str] = []
    for rn, row in enumerate(reader, 2):
        raw = next((row[k] for k in row if k and k.strip().lower() == cui_col), None)
        if not raw or not str(raw).strip():
            warnings.append(f"Row {rn}: empty CUI — skipped")
            continue
        ok, msg, clean = validate_cui(raw)
        if not ok:
            warnings.append(f"Row {rn}: {msg} — skipped")
        else:
            valid.append(clean)

    if not valid:
        return False, "No valid CUIs in file", [], warnings
    return True, "", valid, warnings
