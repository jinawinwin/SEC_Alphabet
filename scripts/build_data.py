"""Turn raw SEC XBRL facts into annual / half-year / quarterly datasets.

Outputs
  data/processed/{annual,half_year,quarterly,peers}.csv
  docs/data/dashboard.json   (consumed by the dashboard)
  README.md peer table       (between PEERS markers)
"""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
DOCS_DATA = ROOT / "docs" / "data"

FIRST_YEAR = 2015
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "20-F", "20-F/A"}
SPLIT_DATE = "2022-07-15"  # 20-for-1 stock split; older filings report pre-split EPS

# key -> (label, section, kind, concepts). kind: flow (duration), stock (instant), eps
RAW_METRICS = {
    "revenue": ("매출 (Revenue)", "income", "flow", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]),
    "cost_of_revenue": ("매출원가 (Cost of revenue)", "income", "flow", ["CostOfRevenue"]),
    "rnd": ("연구개발비 (R&D)", "income", "flow", ["ResearchAndDevelopmentExpense"]),
    "sales_marketing": ("판매·마케팅비 (S&M)", "income", "flow", ["SellingAndMarketingExpense"]),
    "g_and_a": ("일반관리비 (G&A)", "income", "flow", ["GeneralAndAdministrativeExpense"]),
    "operating_income": ("영업이익 (Operating income)", "income", "flow", ["OperatingIncomeLoss"]),
    "other_income": ("영업외손익 (OI&E)", "income", "flow", ["NonoperatingIncomeExpense"]),
    "pretax_income": ("세전이익 (Pre-tax income)", "income", "flow",
                      ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                       "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "income_tax": ("법인세 (Income tax)", "income", "flow", ["IncomeTaxExpenseBenefit"]),
    "net_income": ("순이익 (Net income)", "income", "flow", ["NetIncomeLoss"]),
    "eps_diluted": ("희석 EPS (USD)", "income", "eps", ["EarningsPerShareDiluted"]),
    "operating_cf": ("영업활동현금흐름 (OCF)", "cashflow", "flow", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("설비투자 (CapEx)", "cashflow", "flow", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "buybacks": ("자사주 매입 (Buybacks)", "cashflow", "flow", ["PaymentsForRepurchaseOfCommonStock"]),
    "dividends": ("배당금 지급 (Dividends)", "cashflow", "flow", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
    "sbc": ("주식보상비용 (SBC)", "cashflow", "flow", ["ShareBasedCompensation"]),
    "cash_st_inv": ("현금·단기투자 (Cash & ST inv.)", "balance", "stock", ["CashCashEquivalentsAndShortTermInvestments"]),
    "current_assets": ("유동자산 (Current assets)", "balance", "stock", ["AssetsCurrent"]),
    "total_assets": ("총자산 (Total assets)", "balance", "stock", ["Assets"]),
    "current_liabilities": ("유동부채 (Current liabilities)", "balance", "stock", ["LiabilitiesCurrent"]),
    "total_liabilities": ("총부채 (Total liabilities)", "balance", "stock", ["Liabilities"]),
    "long_term_debt": ("장기차입금 (Long-term debt)", "balance", "stock", ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]),
    "equity": ("자본총계 (Equity)", "balance", "stock", ["StockholdersEquity"]),
}

# Order and labels for tables / CSV, including derived metrics
COLUMNS = [
    ("revenue", "income"), ("cost_of_revenue", "income"), ("gross_profit", "income"), ("rnd", "income"),
    ("sales_marketing", "income"), ("g_and_a", "income"), ("operating_income", "income"),
    ("other_income", "income"), ("pretax_income", "income"), ("income_tax", "income"),
    ("net_income", "income"), ("eps_diluted", "income"),
    ("operating_cf", "cashflow"), ("capex", "cashflow"), ("free_cash_flow", "cashflow"),
    ("buybacks", "cashflow"), ("dividends", "cashflow"), ("sbc", "cashflow"),
    ("cash_st_inv", "balance"), ("current_assets", "balance"), ("total_assets", "balance"),
    ("current_liabilities", "balance"), ("total_liabilities", "balance"), ("long_term_debt", "balance"),
    ("equity", "balance"),
    ("revenue_yoy", "ratio"), ("net_income_yoy", "ratio"), ("gross_margin", "ratio"),
    ("operating_margin", "ratio"), ("net_margin", "ratio"), ("rnd_ratio", "ratio"),
    ("fcf_margin", "ratio"), ("effective_tax_rate", "ratio"), ("current_ratio", "ratio"),
    ("debt_to_equity", "ratio"), ("roe", "ratio"),
]
DERIVED_LABELS = {
    "gross_profit": "매출총이익 (Gross profit)",
    "free_cash_flow": "잉여현금흐름 (FCF)",
    "revenue_yoy": "매출 성장률 YoY",
    "net_income_yoy": "순이익 성장률 YoY",
    "gross_margin": "매출총이익률",
    "operating_margin": "영업이익률",
    "net_margin": "순이익률",
    "rnd_ratio": "R&D / 매출",
    "fcf_margin": "FCF 마진",
    "effective_tax_rate": "유효세율",
    "current_ratio": "유동비율 (배)",
    "debt_to_equity": "장기차입금 / 자본 (배)",
    "roe": "ROE (연환산)",
}
UNITS = {k: "usd" for k, _ in COLUMNS}
UNITS.update({k: "pct" for k, s in COLUMNS if s == "ratio"})
UNITS.update({"eps_diluted": "eps", "current_ratio": "x", "debt_to_equity": "x"})

SECTIONS = {"income": "손익계산서", "cashflow": "현금흐름표", "balance": "재무상태표", "ratio": "주요 비율"}


def label(key):
    return RAW_METRICS[key][0] if key in RAW_METRICS else DERIVED_LABELS[key]


def d(s):
    return date.fromisoformat(s)


def collect(facts, concepts, kind):
    """Map (start, end) or end -> value, keeping the most recently filed figure.

    Concepts are in priority order: a later concept only fills periods the earlier ones lack.
    """
    merged = {}
    gaap = facts["facts"]["us-gaap"]
    for concept in concepts:
        if concept not in gaap:
            continue
        best = {}
        for unit_vals in gaap[concept]["units"].values():
            for f in unit_vals:
                if f.get("form") not in FORMS:
                    continue
                key = f["end"] if kind == "stock" else (f.get("start"), f["end"])
                if kind != "stock" and not f.get("start"):
                    continue
                val = f["val"]
                if kind == "eps" and f["end"] < SPLIT_DATE and f["filed"] < SPLIT_DATE:
                    val = round(val / 20, 4)
                if key not in best or f["filed"] > best[key][1]:
                    best[key] = (val, f["filed"])
        for k, (v, _) in best.items():
            merged.setdefault(k, v)
    return merged


Q_ENDS = ["03-31", "06-30", "09-30", "12-31"]
Q_STARTS = ["01-01", "04-01", "07-01", "10-01"]


def sub(a, b):
    return None if a is None or b is None else a - b


def flow_periods(series, year):
    """Return dict with Q1..Q4, H1, H2, FY for a calendar-year duration series."""
    jan1 = f"{year}-01-01"
    ytd = [series.get((jan1, f"{year}-{e}")) for e in Q_ENDS]
    direct = [series.get((f"{year}-{s}", f"{year}-{e}")) for s, e in zip(Q_STARTS, Q_ENDS)]
    quarters = []
    for i in range(4):
        if direct[i] is not None:
            quarters.append(direct[i])
        elif i == 0:
            quarters.append(ytd[0])
        else:
            quarters.append(sub(ytd[i], ytd[i - 1]))
    # YTD fallback from quarters (e.g. Q4 derived as FY - 9M)
    for i in range(1, 4):
        if ytd[i] is None and all(q is not None for q in quarters[: i + 1]):
            ytd[i] = sum(quarters[: i + 1])
    h1 = ytd[1]
    h2 = sub(ytd[3], ytd[1])
    if h2 is None and quarters[2] is not None and quarters[3] is not None:
        h2 = quarters[2] + quarters[3]
    return {"Q1": quarters[0], "Q2": quarters[1], "Q3": quarters[2], "Q4": quarters[3],
            "H1": h1, "H2": h2, "FY": ytd[3]}


def eps_periods(series, year):
    """EPS is not additive: use reported figures; Q4/H2 approximated by subtraction."""
    jan1 = f"{year}-01-01"
    ytd = [series.get((jan1, f"{year}-{e}")) for e in Q_ENDS]
    direct = [series.get((f"{year}-{s}", f"{year}-{e}")) for s, e in zip(Q_STARTS, Q_ENDS)]
    q = [direct[0] or ytd[0], direct[1], direct[2], direct[3] or (sub(ytd[3], ytd[2]))]
    r = lambda v: None if v is None else round(v, 2)
    return {"Q1": r(q[0]), "Q2": r(q[1]), "Q3": r(q[2]), "Q4": r(q[3]),
            "H1": r(ytd[1]), "H2": r(sub(ytd[3], ytd[1])), "FY": r(ytd[3])}


def stock_periods(series, year):
    v = {f"Q{i + 1}": series.get(f"{year}-{e}") for i, e in enumerate(Q_ENDS)}
    v.update({"H1": v["Q2"], "H2": v["Q4"], "FY": v["Q4"]})
    return v


def ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def add_derived(rows, periods_per_year):
    by_label = {r["period"]: r for r in rows}
    for r in rows:
        r["gross_profit"] = sub(r["revenue"], r["cost_of_revenue"])
        r["free_cash_flow"] = sub(r["operating_cf"], r["capex"])
        r["gross_margin"] = ratio(r["gross_profit"], r["revenue"])
        r["operating_margin"] = ratio(r["operating_income"], r["revenue"])
        r["net_margin"] = ratio(r["net_income"], r["revenue"])
        r["rnd_ratio"] = ratio(r["rnd"], r["revenue"])
        r["fcf_margin"] = ratio(r["free_cash_flow"], r["revenue"])
        r["effective_tax_rate"] = ratio(r["income_tax"], r["pretax_income"])
        r["current_ratio"] = ratio(r["current_assets"], r["current_liabilities"])
        r["debt_to_equity"] = ratio(r["long_term_debt"], r["equity"])
        prev = by_label.get(r["prior_period"])
        r["revenue_yoy"] = ratio(sub(r["revenue"], prev and prev["revenue"]), prev and prev["revenue"])
        r["net_income_yoy"] = ratio(sub(r["net_income"], prev and prev["net_income"]), prev and prev["net_income"])
        prev_eq = rows[rows.index(r) - 1]["equity"] if rows.index(r) > 0 else None
        avg_eq = (r["equity"] + prev_eq) / 2 if r["equity"] is not None and prev_eq is not None else r["equity"]
        ni = r["net_income"]
        r["roe"] = ratio(ni * periods_per_year if ni is not None else None, avg_eq)


def build_alphabet():
    facts = json.loads((RAW / "alphabet_companyfacts.json").read_text(encoding="utf-8"))
    series = {k: collect(facts, concepts, kind) for k, (_, _, kind, concepts) in RAW_METRICS.items()}
    last_year = max(int(k[1][:4]) for k in series["revenue"])

    annual, half, quarterly = [], [], []
    for year in range(FIRST_YEAR, last_year + 1):
        per = {}
        for key, (_, _, kind, _) in RAW_METRICS.items():
            fn = {"flow": flow_periods, "eps": eps_periods, "stock": stock_periods}[kind]
            per[key] = fn(series[key], year)

        def row(tag, label_, end, prior):
            return {"period": label_, "fiscal_year": year, "period_end": end, "prior_period": prior,
                    **{k: per[k][tag] for k in RAW_METRICS}}

        annual.append(row("FY", f"FY{year}", f"{year}-12-31", f"FY{year - 1}"))
        half.append(row("H1", f"{year} H1", f"{year}-06-30", f"{year - 1} H1"))
        half.append(row("H2", f"{year} H2", f"{year}-12-31", f"{year - 1} H2"))
        for i, e in enumerate(Q_ENDS):
            quarterly.append(row(f"Q{i + 1}", f"{year} Q{i + 1}", f"{year}-{e}", f"{year - 1} Q{i + 1}"))

    # drop periods that are not reported yet
    keep = lambda rows: [r for r in rows if r["revenue"] is not None]
    annual, half, quarterly = keep(annual), keep(half), keep(quarterly)
    add_derived(annual, 1)
    add_derived(half, 2)
    add_derived(quarterly, 4)
    return facts, annual, half, quarterly


# ---------------------------------------------------------------- peers
PEER_META = [
    # ticker, name, country, listing, overlap with Alphabet, SEC filer
    ("MSFT", "Microsoft", "미국", "NASDAQ", "클라우드(Azure), 검색(Bing), 생성형 AI, 생산성 SW", True),
    ("AAPL", "Apple", "미국", "NASDAQ", "모바일 OS(iOS vs Android), 앱스토어, 하드웨어", True),
    ("AMZN", "Amazon", "미국", "NASDAQ", "클라우드(AWS), 디지털 광고, 스마트 기기", True),
    ("META", "Meta Platforms", "미국", "NASDAQ", "디지털 광고, 소셜/동영상(YouTube 경쟁), AI 모델", True),
    ("ORCL", "Oracle", "미국", "NYSE", "클라우드 인프라(OCI), 데이터베이스", True),
    ("NVDA", "NVIDIA", "미국", "NASDAQ", "AI 가속기(TPU vs GPU), AI 인프라", True),
    ("BABA", "Alibaba Group", "중국", "NYSE / HKEX", "클라우드(Alibaba Cloud), 전자상거래 광고, AI 모델", True),
    ("BIDU", "Baidu", "중국", "NASDAQ / HKEX", "검색 광고, 자율주행(Waymo vs Apollo), AI 모델", True),
    ("0700.HK", "Tencent", "중국", "HKEX", "디지털 광고, 클라우드, 게임·콘텐츠", False),
    ("035420.KS", "NAVER", "한국", "KRX", "검색 광고, 클라우드, 커머스", False),
]

PEER_CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"],
}


def latest_annual(facts, concepts):
    """Latest full-year USD fact across candidate concepts -> (start, end, val)."""
    best = None
    for concept in concepts:
        unit_vals = facts["facts"].get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD", [])
        for f in unit_vals:
            if f.get("form") not in ("10-K", "20-F", "10-K/A", "20-F/A") or not f.get("start"):
                continue
            if not 350 <= (d(f["end"]) - d(f["start"])).days <= 380:
                continue
            cand = (f["end"], f["filed"], f["start"], f["val"])
            if best is None or cand[:2] > best[:2]:
                best = cand
    return None if best is None else (best[2], best[0], best[3])


def build_peers(alphabet_facts):
    rows = []
    entries = [("GOOGL", "Alphabet (기준)", "미국", "NASDAQ", "검색·YouTube 광고, Google Cloud, Android, Waymo", True)]
    for ticker, name, country, listing, overlap, sec in [entries[0]] + PEER_META:
        rec = {"ticker": ticker, "name": name, "country": country, "listing": listing, "overlap": overlap,
               "sec_filer": sec, "fiscal_year_end": None, "revenue": None, "operating_income": None,
               "net_income": None, "rnd": None, "capex": None}
        facts = alphabet_facts if ticker == "GOOGL" else None
        if sec and facts is None:
            path = RAW / "peers" / f"{ticker}_companyfacts.json"
            if path.exists():
                facts = json.loads(path.read_text(encoding="utf-8"))
        if facts is not None:
            rev = latest_annual(facts, PEER_CONCEPTS["revenue"])
            if rev:
                rec["fiscal_year_end"] = rev[1]
                rec["revenue"] = rev[2]
                for key, concepts in PEER_CONCEPTS.items():
                    if key == "revenue":
                        continue
                    hit = latest_annual(facts, concepts)
                    if hit and hit[1] == rev[1]:  # same fiscal year only
                        rec[key] = hit[2]
        rec["operating_margin"] = ratio(rec["operating_income"], rec["revenue"])
        rec["net_margin"] = ratio(rec["net_income"], rec["revenue"])
        rec["rnd_ratio"] = ratio(rec["rnd"], rec["revenue"])
        rows.append(rec)
    return rows


# ---------------------------------------------------------------- outputs
def write_csv(path, rows):
    keys = ["period", "fiscal_year", "period_end"] + [k for k, _ in COLUMNS]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(keys)
        for r in rows:
            w.writerow([r.get(k) if not isinstance(r.get(k), float) else round(r[k], 6) for k in keys])


def fmt_b(v):
    return "—" if v is None else f"{v / 1e9:,.1f}"


def fmt_pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def update_readme(peers, updated):
    path = ROOT / "README.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start, end = "<!-- PEERS:START -->", "<!-- PEERS:END -->"
    if start not in text or end not in text:
        return
    lines = [
        f"_SEC XBRL 최신 연간 보고서(10-K / 20-F) 기준 · 단위: 십억 달러(USD bn) · 갱신: {updated}_",
        "",
        "| 기업 | 티커 | 국가 | 상장 | Alphabet과의 경쟁 영역 | 회계연도 말 | 매출 | 영업이익률 | 순이익 | R&D/매출 |",
        "|---|---|---|---|---|---|--:|--:|--:|--:|",
    ]
    for p in peers:
        lines.append(
            f"| {p['name']} | {p['ticker']} | {p['country']} | {p['listing']} | {p['overlap']} | "
            f"{p['fiscal_year_end'] or 'SEC 미제출'} | {fmt_b(p['revenue'])} | {fmt_pct(p['operating_margin'])} | "
            f"{fmt_b(p['net_income'])} | {fmt_pct(p['rnd_ratio'])} |"
        )
    block = start + "\n" + "\n".join(lines) + "\n" + end
    path.write_text(text[: text.index(start)] + block + text[text.index(end) + len(end):], encoding="utf-8")


def recent_filings():
    sub_path = RAW / "alphabet_submissions.json"
    if not sub_path.exists():
        return []
    recent = json.loads(sub_path.read_text(encoding="utf-8"))["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if form not in ("10-K", "10-Q"):
            continue
        acc = recent["accessionNumber"][i]
        out.append({
            "form": form, "filed": recent["filingDate"][i], "period": recent["reportDate"][i],
            "url": f"https://www.sec.gov/Archives/edgar/data/1652044/{acc.replace('-', '')}/{recent['primaryDocument'][i]}",
        })
        if len(out) == 8:
            break
    return out


def clean(rows):
    out = []
    for r in rows:
        c = {k: v for k, v in r.items() if k != "prior_period"}
        for k, v in c.items():
            if isinstance(v, float):
                c[k] = round(v, 6)
        out.append(c)
    return out


def main():
    PROCESSED.mkdir(parents=True, exist_ok=True)
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    facts, annual, half, quarterly = build_alphabet()
    peers = build_peers(facts)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    write_csv(PROCESSED / "annual.csv", annual)
    write_csv(PROCESSED / "half_year.csv", half)
    write_csv(PROCESSED / "quarterly.csv", quarterly)
    with (PROCESSED / "peers.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(peers[0].keys()))
        w.writeheader()
        w.writerows(peers)

    dashboard = {
        "meta": {
            "company": facts["entityName"], "ticker": "GOOGL / GOOG", "cik": "0001652044",
            "updated": updated, "latest_quarter": quarterly[-1]["period"], "latest_quarter_end": quarterly[-1]["period_end"],
            "latest_fy": annual[-1]["period"] if annual[-1]["revenue"] and annual[-1]["period_end"] <= quarterly[-1]["period_end"] else annual[-2]["period"],
            "source": "SEC EDGAR XBRL companyfacts API",
        },
        "columns": [{"key": k, "label": label(k), "section": s, "unit": UNITS[k]} for k, s in COLUMNS],
        "sections": SECTIONS,
        "annual": clean(annual), "half_year": clean(half), "quarterly": clean(quarterly),
        "peers": peers, "filings": recent_filings(),
    }
    # an in-progress year has no FY total yet: keep only completed years in the annual set
    dashboard["annual"] = [r for r in dashboard["annual"] if r["period_end"] <= quarterly[-1]["period_end"]]
    (DOCS_DATA / "dashboard.json").write_text(json.dumps(dashboard, ensure_ascii=False, indent=1), encoding="utf-8")
    update_readme(peers, updated)
    print(f"annual={len(dashboard['annual'])} half={len(half)} quarterly={len(quarterly)} peers={len(peers)}; "
          f"latest quarter {quarterly[-1]['period']}")


if __name__ == "__main__":
    main()
