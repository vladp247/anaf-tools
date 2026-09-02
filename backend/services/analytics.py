"""Analytics engine: bulk statistics with CAGR and anchor-year support."""
from __future__ import annotations
import statistics
from typing import Any


def _safe(vals: list, fn) -> float | None:
    try:
        return round(fn(vals), 2) if vals else None
    except Exception:
        return None


def compute_cagr(start_val, end_val, n_years: int) -> float | None:
    if not start_val or not end_val or n_years <= 0:
        return None
    if start_val <= 0 or end_val <= 0:
        return None
    try:
        return round(((end_val / start_val) ** (1.0 / n_years) - 1.0) * 100.0, 2)
    except Exception:
        return None


def _per_company_cagr(fins, yrs):
    rev_yrs = sorted(y for y in yrs if fins.get(y) and (fins[y].get("net_turnover") or 0) > 0)
    cagr_rev = sy_rev = ey_rev = None
    if len(rev_yrs) >= 2:
        sy_rev, ey_rev = rev_yrs[0], rev_yrs[-1]
        cagr_rev = compute_cagr(fins[sy_rev]["net_turnover"], fins[ey_rev]["net_turnover"], ey_rev - sy_rev)
    net_yrs = sorted(y for y in yrs if fins.get(y) and (fins[y].get("net_result") or 0) > 0)
    cagr_net = None
    if len(net_yrs) >= 2:
        sy_n, ey_n = net_yrs[0], net_yrs[-1]
        cagr_net = compute_cagr(fins[sy_n]["net_result"], fins[ey_n]["net_result"], ey_n - sy_n)
    return cagr_rev, cagr_net, sy_rev, ey_rev


def compute_analytics(results: list, years: list, anchor_year: int | None = None) -> dict:
    if not results:
        return {"empty": True}
    yrs = sorted(years)
    eff_anchor = anchor_year if (anchor_year and anchor_year in yrs) else max(yrs)
    rows, rev_by_yr, net_by_yr, emp_by_yr = [], {y: [] for y in yrs}, {y: [] for y in yrs}, {y: [] for y in yrs}
    sectors, counties = {}, {}
    profitable = loss = no_rev = inactive = vat_reg = 0
    port_cagr_revs, port_cagr_nets = [], []

    for res in results:
        co = res.get("company") or {}
        fins = res.get("financials") or {}
        if co.get("is_inactive"):       inactive += 1
        if co.get("is_vat_registered"): vat_reg  += 1
        any_fin = next((f for f in fins.values() if f), None)
        caen = (any_fin.get("caen_description") or "") if any_fin else ""
        if caen: sectors[caen] = sectors.get(caen, 0) + 1
        addr   = co.get("address_registered_office") or {}
        county = addr.get("county", "")
        if county: counties[county] = counties.get(county, 0) + 1

        for y in yrs:
            fd = fins.get(y)
            if not fd: continue
            if fd.get("net_turnover"):          rev_by_yr[y].append(fd["net_turnover"])
            if fd.get("net_result") is not None: net_by_yr[y].append(fd["net_result"])
            if fd.get("avg_employees"):         emp_by_yr[y].append(fd["avg_employees"])

        anchor_fd = fins.get(eff_anchor)
        best_fd   = anchor_fd or next((fins[y] for y in reversed(yrs) if fins.get(y)), None)

        cagr_rev, cagr_net, cagr_sy, cagr_ey = _per_company_cagr(fins, yrs)
        if cagr_rev is not None: port_cagr_revs.append(cagr_rev)
        if cagr_net is not None: port_cagr_nets.append(cagr_net)

        if best_fd:
            net = best_fd.get("net_result") or 0
            rev = best_fd.get("net_turnover") or 0
            if net > 0:   profitable += 1
            elif net < 0: loss       += 1
            if not rev:   no_rev     += 1
            rows.append({
                "cui": res["cui"], "name": co.get("name", f"CUI {res['cui']}"),
                "revenue": rev, "net_result": net,
                "margin_pct": best_fd.get("profit_margin_pct"),
                "employees": best_fd.get("avg_employees") or 0,
                "year": best_fd.get("year"), "county": county, "caen": caen,
                "is_profitable": net > 0, "is_inactive": co.get("is_inactive", False),
                "cagr_revenue": cagr_rev, "cagr_net": cagr_net,
                "cagr_start_year": cagr_sy, "cagr_end_year": cagr_ey,
            })
        else:
            no_rev += 1

    all_rev = [r["revenue"]    for r in rows if r["revenue"]]
    all_net = [r["net_result"] for r in rows if r["net_result"]]
    all_emp = [r["employees"]  for r in rows if r["employees"]]
    top_rev  = sorted(rows, key=lambda r: r["revenue"],    reverse=True)[:10]
    top_prof = sorted(rows, key=lambda r: r["net_result"], reverse=True)[:10]
    bot_prof = sorted(rows, key=lambda r: r["net_result"])[:10]
    trend_rev = [{"year": y, "avg_revenue": _safe(rev_by_yr[y], statistics.mean),
                  "median_revenue": _safe(rev_by_yr[y], statistics.median),
                  "total_revenue": round(sum(rev_by_yr[y])) if rev_by_yr[y] else None,
                  "count": len(rev_by_yr[y])} for y in yrs]
    trend_net = [{"year": y, "avg_net": _safe(net_by_yr[y], statistics.mean),
                  "count": len(net_by_yr[y])} for y in yrs]
    cagr_sy_all = min((r["cagr_start_year"] for r in rows if r.get("cagr_start_year")), default=None)
    cagr_ey_all = max((r["cagr_end_year"]   for r in rows if r.get("cagr_end_year")),   default=None)

    return {
        "total_results": len(results), "companies_with_financials": len(rows),
        "profitable_count": profitable, "loss_count": loss,
        "no_revenue_count": no_rev, "inactive_count": inactive,
        "vat_registered_count": vat_reg, "anchor_year": eff_anchor, "available_years": yrs,
        "revenue_stats": {"avg": _safe(all_rev, statistics.mean), "median": _safe(all_rev, statistics.median),
                          "total": round(sum(all_rev)) if all_rev else None,
                          "min": min(all_rev) if all_rev else None, "max": max(all_rev) if all_rev else None},
        "net_result_stats": {"avg": _safe(all_net, statistics.mean), "median": _safe(all_net, statistics.median),
                             "total": round(sum(all_net)) if all_net else None,
                             "min": min(all_net) if all_net else None, "max": max(all_net) if all_net else None},
        "employee_stats": {"avg": _safe(all_emp, statistics.mean), "median": _safe(all_emp, statistics.median),
                           "total": int(sum(all_emp)) if all_emp else None},
        "portfolio_cagr": {
            "revenue_median": _safe(port_cagr_revs, statistics.median),
            "revenue_avg":    _safe(port_cagr_revs, statistics.mean),
            "net_median":     _safe(port_cagr_nets, statistics.median),
            "net_avg":        _safe(port_cagr_nets, statistics.mean),
            "companies_count": len(port_cagr_revs),
            "start_year": cagr_sy_all, "end_year": cagr_ey_all,
        },
        "revenue_trend": trend_rev, "net_trend": trend_net,
        "top_by_revenue": top_rev, "top_by_profit": top_prof, "bottom_by_profit": bot_prof,
        "sector_distribution": [{"label": k, "count": v} for k, v in sorted(sectors.items(), key=lambda x: -x[1])[:15]],
        "county_distribution": [{"label": k, "count": v} for k, v in sorted(counties.items(), key=lambda x: -x[1])[:15]],
        "years_analyzed": yrs,
    }
