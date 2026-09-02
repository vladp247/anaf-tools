"""Normalizer: raw ANAF JSON → clean Python dicts."""
from __future__ import annotations
from typing import Any


def _s(v: Any) -> str:
    if v is None: return ""
    return str(v).strip()

def _b(v: Any) -> bool:
    if isinstance(v, bool): return v
    return str(v).lower() in ("true", "1", "yes")

def _i(v: Any, default: int = 0) -> int:
    try: return int(v) if v is not None else default
    except: return default


def normalize_company_info(raw: dict) -> dict:
    dg = raw.get("date_generale") or {}
    tva = raw.get("inregistrare_scop_Tva") or {}
    rtvai = raw.get("inregistrare_RTVAI") or {}
    inactiv = raw.get("stare_inactiv") or {}
    split = raw.get("inregistrare_SplitTVA") or {}
    sediu = raw.get("adresa_sediu_social") or {}
    dom = raw.get("adresa_domiciliu_fiscal") or {}

    tva_periods = [
        {
            "start": _s(p.get("data_inceput_ScpTVA")),
            "end": _s(p.get("data_sfarsit_ScpTVA")),
            "cancellation_date": _s(p.get("data_anul_imp_ScpTVA")),
            "reason": _s(p.get("mesaj_ScpTVA")),
        }
        for p in (tva.get("perioade_TVA") or [])
    ]

    def addr(d: dict, p: str) -> dict:
        return {
            "street": _s(d.get(f"{p}denumire_Strada")),
            "number": _s(d.get(f"{p}numar_Strada")),
            "locality": _s(d.get(f"{p}denumire_Localitate")),
            "locality_code": _s(d.get(f"{p}cod_Localitate")),
            "county": _s(d.get(f"{p}denumire_Judet")),
            "county_code": _s(d.get(f"{p}cod_Judet")),
            "county_auto": _s(d.get(f"{p}cod_JudetAuto")),
            "details": _s(d.get(f"{p}detalii_Adresa")),
            "postal_code": _s(d.get(f"{p}cod_Postal")),
            "country": _s(d.get(f"{p}tara") or d.get("stara")),
        }

    return {
        "cui": _i(dg.get("cui")),
        "name": _s(dg.get("denumire")),
        "registration_number": _s(dg.get("nrRegCom")),
        "registration_date": _s(dg.get("data_inregistrare")),
        "caen_code": _s(dg.get("cod_CAEN")),
        "legal_form": _s(dg.get("forma_juridica")),
        "organization_form": _s(dg.get("forma_organizare")),
        "ownership_form": _s(dg.get("forma_de_proprietate")),
        "fiscal_authority": _s(dg.get("organFiscalCompetent")),
        "registration_status": _s(dg.get("stare_inregistrare")),
        "phone": _s(dg.get("telefon")),
        "fax": _s(dg.get("fax")),
        "postal_code": _s(dg.get("codPostal")),
        "address_full": _s(dg.get("adresa")),
        "iban": _s(dg.get("iban")),
        "auth_document": _s(dg.get("act")),
        "is_vat_registered": _b(tva.get("scpTVA")),
        "is_inactive": _b(inactiv.get("statusInactivi")),
        "is_cash_vat": _b(rtvai.get("statusTvaIncasare")),
        "is_split_vat": _b(split.get("statusSplitTVA")),
        "is_ro_efactura": _b(dg.get("statusRO_e_Factura")),
        "vat_periods": tva_periods,
        "inactivation_date": _s(inactiv.get("dataInactivare")),
        "reactivation_date": _s(inactiv.get("dataReactivare")),
        "inactivation_publish_date": _s(inactiv.get("dataPublicare")),
        "deletion_date": _s(inactiv.get("dataRadiere")),
        "cash_vat_start": _s(rtvai.get("dataInceputTvaInc")),
        "cash_vat_end": _s(rtvai.get("dataSfarsitTvaInc")),
        "split_vat_start": _s(split.get("dataInceputSplitTVA")),
        "split_vat_end": _s(split.get("dataAnulareSplitTVA")),
        "address_registered_office": addr(sediu, "s"),
        "address_fiscal_domicile": addr(dom, "d"),
    }


def normalize_financials(raw: dict) -> dict | None:
    indicators_raw = raw.get("i") or []
    if not indicators_raw:
        return None
    ind: dict[str, int] = {}
    for item in indicators_raw:
        k = str(item.get("indicator", "")).strip()
        v = item.get("val_indicator")
        try: ind[k] = int(v) if v is not None else 0
        except: ind[k] = 0

    def g(k: str) -> int: return ind.get(k, 0)

    rev = g("I13")
    net_p = g("I18"); net_l = g("I19")
    gr_p = g("I16"); gr_l = g("I17")
    net_result = net_p - net_l
    gr_result = gr_p - gr_l
    margin = round(net_result / rev * 100, 2) if rev else None

    return {
        "year": _i(raw.get("an")),
        "caen_code": _s(raw.get("caen")) if raw.get("caen") else "",
        "caen_description": _s(raw.get("den_caen")),
        "company_name_api": _s(raw.get("deni")),
        "fixed_assets": g("I1"),
        "current_assets": g("I2"),
        "inventories": g("I3"),
        "receivables": g("I4"),
        "cash_and_bank": g("I5"),
        "prepaid_expenses": g("I6"),
        "liabilities": g("I7"),
        "deferred_income": g("I8"),
        "provisions": g("I9"),
        "total_equity": g("I10"),
        "paid_in_capital": g("I11"),
        "state_patrimony": g("I12"),
        "net_turnover": rev,
        "total_revenue": g("I14"),
        "total_expenses": g("I15"),
        "gross_profit": gr_p,
        "gross_loss": gr_l,
        "net_profit": net_p,
        "net_loss": net_l,
        "avg_employees": g("I20"),
        "net_result": net_result,
        "gross_result": gr_result,
        "profit_margin_pct": margin,
        "is_profitable": net_result > 0,
        "_raw": {item["indicator"]: item.get("val_indicator", 0) for item in indicators_raw},
    }


def normalize_full(cui: int, co: dict | None, fins: dict[int, dict | None], years: list[int]) -> dict:
    has_co = co is not None
    has_fin = any(f is not None for f in fins.values())
    return {
        "cui": cui,
        "status": "success" if (has_co or has_fin) else "no_data",
        "company": co,
        "financials": fins,
        "years_requested": years,
        "years_with_data": [y for y, f in fins.items() if f is not None],
        "has_company_info": has_co,
        "has_financials": has_fin,
    }


def normalize_financials_from_db(row, year: int, caen: int) -> dict | None:
    """Convert a caen_financials SQLite row to the same structure as normalize_financials().
    Row columns: id, cui, caen, year, source, from_live_scan, i1..i20"""
    def g(col: str) -> int:
        try: return int(row[col] or 0)
        except: return 0

    rev       = g("i13")
    net_p     = g("i18"); net_l = g("i19")
    gr_p      = g("i16"); gr_l  = g("i17")
    net_result = net_p - net_l
    gr_result  = gr_p  - gr_l
    margin     = round(net_result / rev * 100, 2) if rev else None

    # Return None if all key indicators are zero (company not in this year's files)
    if not rev and not net_p and not net_l and not g("i7") and not g("i10"):
        return None

    return {
        "year": year,
        "caen_code": str(caen),
        "caen_description": "",        # not stored per-row in txt files
        "company_name_api": "",
        "fixed_assets":    g("i1"),
        "current_assets":  g("i2"),
        "inventories":     g("i3"),
        "receivables":     g("i4"),
        "cash_and_bank":   g("i5"),
        "prepaid_expenses":g("i6"),
        "liabilities":     g("i7"),
        "deferred_income": g("i8"),
        "provisions":      g("i9"),
        "total_equity":    g("i10"),
        "paid_in_capital": g("i11"),
        "state_patrimony": g("i12"),
        "net_turnover":    rev,
        "total_revenue":   g("i14"),
        "total_expenses":  g("i15"),
        "gross_profit":    gr_p,
        "gross_loss":      gr_l,
        "net_profit":      net_p,
        "net_loss":        net_l,
        "avg_employees":   g("i20"),
        "net_result":      net_result,
        "gross_result":    gr_result,
        "profit_margin_pct": margin,
        "is_profitable":   net_result > 0,
        "_raw": {f"I{i}": row[f"i{i}"] for i in range(1, 21)},
        "_source": "offline",
    }


def normalize_company_from_onrc(firma: dict, caen: int) -> dict:
    """Build a company dict from ONRC firme row — same structure as normalize_company_info().
    Used in offline mode when ONRC is indexed (no ANAF company-info call needed)."""
    return {
        "cui":                  firma.get("cui", 0),
        "name":                 firma.get("denumire", ""),
        "registration_number":  firma.get("cod_inmatriculare", ""),
        "registration_date":    firma.get("data_inmatriculare", ""),
        "caen_code":            str(caen),
        "legal_form":           firma.get("forma_juridica", ""),
        "organization_form":    "",
        "ownership_form":       "",
        "fiscal_authority":     "",
        "registration_status":  "",
        "phone":                "",
        "fax":                  "",
        "postal_code":          firma.get("adr_cod_postal", ""),
        "address_full":         "",
        "iban":                 "",
        "auth_document":        "",
        "is_vat_registered":    None,   # not available offline
        "is_inactive":          None,
        "is_cash_vat":          False,
        "is_split_vat":         False,
        "is_ro_efactura":       False,
        "vat_periods":          [],
        "inactivation_date":    "",
        "reactivation_date":    "",
        "inactivation_publish_date": "",
        "deletion_date":        "",
        "cash_vat_start":       "",
        "cash_vat_end":         "",
        "split_vat_start":      "",
        "split_vat_end":        "",
        "address_registered_office": {
            "street":      firma.get("adr_den_strada", ""),
            "number":      firma.get("adr_nr_strada", ""),
            "locality":    firma.get("adr_localitate", ""),
            "locality_code": "",
            "county":      firma.get("adr_judet", ""),
            "county_code": "",
            "county_auto": "",
            "details":     "",
            "postal_code": firma.get("adr_cod_postal", ""),
            "country":     firma.get("tara_firma_mama", ""),
        },
        "address_fiscal_domicile": {},
        "_source": "onrc_offline",
    }
