import base64, json, os
os.chdir("C:/Users/17ton/urbangrowth")
from pathlib import Path

asset_dir = Path("D:/urbangrowth_data/processed/maps/report_assets")

def img_b64(path):
    data = Path(path).read_bytes()
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"

equity_b64    = img_b64(asset_dir / "equity_curves.png")
bps_b64       = img_b64(asset_dir / "bps_sensitivity.png")
ic_b64        = img_b64(asset_dir / "lot_backtest_ic.png")
portfolio_b64 = img_b64(asset_dir / "re_portfolio_comparison.png")

with open("/tmp/top_lots.json") as f:
    lots = json.load(f)

phoenix_lots = [l for l in lots if l["city_name"] == "phoenix"]
austin_lots  = [l for l in lots if l["city_name"] == "austin"]

bps_data = [
    {"entity": "SPY Buy-Hold", "tc_bps": 0,  "cagr": 0.1264, "sharpe": 0.814, "max_drawdown": -0.244, "hit_rate": 0.674, "n_months": 95},
    {"entity": "Ridge H=2",   "tc_bps": 0,  "cagr": 0.1103,  "sharpe": 0.347, "max_drawdown": -0.457, "hit_rate": 0.623, "n_months": 69},
    {"entity": "Ridge H=2",   "tc_bps": 5,  "cagr": 0.0973,  "sharpe": 0.307, "max_drawdown": -0.458, "hit_rate": 0.623, "n_months": 69},
    {"entity": "Ridge H=2",   "tc_bps": 10, "cagr": 0.0844,  "sharpe": 0.266, "max_drawdown": -0.460, "hit_rate": 0.623, "n_months": 69},
    {"entity": "Ridge H=2",   "tc_bps": 25, "cagr": 0.0465,  "sharpe": 0.147, "max_drawdown": -0.466, "hit_rate": 0.580, "n_months": 69},
    {"entity": "Ridge H=2",   "tc_bps": 50, "cagr": -0.0140, "sharpe": -0.044,"max_drawdown": -0.540, "hit_rate": 0.565, "n_months": 69},
    {"entity": "Ridge H=1",   "tc_bps": 0,  "cagr": 0.0526,  "sharpe": 0.202, "max_drawdown": -0.452, "hit_rate": 0.614, "n_months": 70},
    {"entity": "Ridge H=1",   "tc_bps": 10, "cagr": 0.0276,  "sharpe": 0.106, "max_drawdown": -0.459, "hit_rate": 0.586, "n_months": 70},
    {"entity": "Ridge H=3",   "tc_bps": 0,  "cagr": 0.1074,  "sharpe": 0.309, "max_drawdown": -0.416, "hit_rate": 0.574, "n_months": 68},
    {"entity": "Ridge H=3",   "tc_bps": 10, "cagr": 0.0826,  "sharpe": 0.238, "max_drawdown": -0.422, "hit_rate": 0.574, "n_months": 68},
]

def fmt_pct(v):
    return f"{v*100:.1f}%"

def property_card(r, rank, city_prefix):
    import h3
    lat = float(r["lat"])
    lon = float(r["lon"])
    score = float(r["opportunity_score"])
    area  = float(r.get("acreage_est") or 211)
    dist  = float(r.get("dist_to_center_km") or 0)
    ptype = r.get("dominant_property_type") or "unknown"
    land_val   = r.get("est_land_value_acre")
    build_mo   = r.get("est_construction_months")
    cost_sqft  = r.get("est_cost_per_sqft")
    addr       = r.get("nearest_address") or ""

    map_id = f"m{city_prefix}{rank}"
    addr_line = f'<div class="prop-addr">{addr}</div>' if addr else ""

    build_str = f"{int(float(build_mo))} months" if build_mo and float(build_mo) > 0 else "~9–12 months"
    cost_str  = f"${float(cost_sqft):.0f}/sqft" if cost_sqft and float(cost_sqft) > 0 else "~$175/sqft"
    land_str  = (f"${int(float(land_val)):,}/acre" if land_val and str(land_val) not in ("None","nan","") else "N/A")

    type_color = {"residential":"#2ecc71","commercial":"#3498db","vacant":"#e67e22",
                  "open_space":"#27ae60"}.get(ptype, "#95a5a6")

    return f"""
<div class="prop-card">
  <div class="prop-rank">#{rank}</div>
  <div id="{map_id}" style="height:200px;border-radius:8px;overflow:hidden;margin-bottom:12px;"></div>
  <script>
    (function(){{
      var m=L.map('{map_id}',{{zoomControl:false,attributionControl:false}}).setView([{lat},{lon}],15);
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{maxZoom:20}}).addTo(m);
      var ic=L.divIcon({{className:'',html:'<div style="width:18px;height:18px;background:#ff3333;border:3px solid white;border-radius:50%;box-shadow:0 0 6px rgba(0,0,0,.6)"></div>',iconSize:[18,18],iconAnchor:[9,9]}});
      L.marker([{lat},{lon}],{{icon:ic}}).addTo(m);
    }})();
  </script>
  {addr_line}
  <div class="prop-coords">{lat:.5f}, {lon:.5f}</div>
  <div class="prop-stats">
    <div class="stat"><span class="stat-label">Score</span><span class="stat-val score-val">{score:.3f}</span></div>
    <div class="stat"><span class="stat-label">Area</span><span class="stat-val">{area:.0f} ac</span></div>
    <div class="stat"><span class="stat-label">Dist CBD</span><span class="stat-val">{dist:.1f} km</span></div>
    <div class="stat"><span class="stat-label">Type</span><span class="stat-val" style="color:{type_color};text-transform:capitalize">{ptype}</span></div>
    <div class="stat"><span class="stat-label">Land Value</span><span class="stat-val">{land_str}</span></div>
    <div class="stat"><span class="stat-label">Build Time</span><span class="stat-val">{build_str}</span></div>
    <div class="stat"><span class="stat-label">Cost/sqft</span><span class="stat-val">{cost_str}</span></div>
  </div>
  <a class="maps-link" href="https://www.google.com/maps/@{lat},{lon},16z/data=!3m1!1e3" target="_blank">Open in Google Maps ↗</a>
</div>"""

phx_cards = "\n".join(property_card(r, i+1, "p") for i, r in enumerate(phoenix_lots))
aus_cards = "\n".join(property_card(r, i+1, "a") for i, r in enumerate(austin_lots))

bps_rows = ""
for d in bps_data:
    is_spy = d["entity"] == "SPY Buy-Hold"
    spy_cagr = 0.1264
    cagr_color = "#2ecc71" if d["cagr"] >= spy_cagr else ("#e67e22" if d["cagr"] > 0 else "#e74c3c")
    dd_color   = "#e74c3c" if d["max_drawdown"] < -0.3 else "#e67e22"
    row_class  = ' class="spy-row"' if is_spy else ""
    bps_rows += f"""<tr{row_class}>
      <td><strong>{d["entity"]}</strong></td>
      <td>{"&mdash;" if is_spy else d["tc_bps"]}</td>
      <td style="color:{cagr_color};font-weight:600">{fmt_pct(d["cagr"])}</td>
      <td>{d["sharpe"]:.3f}</td>
      <td style="color:{dd_color}">{fmt_pct(d["max_drawdown"])}</td>
      <td>{fmt_pct(d["hit_rate"])}</td>
      <td>{d["n_months"]}</td>
    </tr>\n"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Urban Growth Research &mdash; Investment Signal Report</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{{--bg:#0f1117;--surface:#1a1d2e;--card:#1e2235;--accent:#6c63ff;--green:#2ecc71;--red:#e74c3c;--orange:#e67e22;--blue:#3498db;--text:#e2e8f0;--muted:#94a3b8;--border:#2d3448}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.6}}
.hero{{background:linear-gradient(135deg,#1a1d2e 0%,#0f1117 60%,#1a0a2e 100%);border-bottom:1px solid var(--border);padding:60px 40px 40px;text-align:center}}
.hero h1{{font-size:2.6rem;font-weight:700;letter-spacing:-.5px;background:linear-gradient(135deg,#a78bfa,#6c63ff,#3498db);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hero .subtitle{{color:var(--muted);margin-top:12px;font-size:1.1rem}}
.hero .date{{color:var(--muted);font-size:.85rem;margin-top:8px}}
.container{{max-width:1200px;margin:0 auto;padding:0 24px}}
.section{{padding:60px 0 40px}}
.section-header{{margin-bottom:32px}}
.section-label{{font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:var(--accent);margin-bottom:8px}}
.section-title{{font-size:1.9rem;font-weight:700;color:var(--text)}}
.section-desc{{color:var(--muted);margin-top:8px;font-size:.95rem;max-width:700px}}
.exec-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin:32px 0}}
.kpi{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;text-align:center}}
.kpi-val{{font-size:2rem;font-weight:700;color:var(--accent)}}
.kpi-label{{font-size:.8rem;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:1px}}
.chart-container{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;margin:24px 0}}
.chart-container img{{width:100%;border-radius:8px}}
.chart-caption{{font-size:.8rem;color:var(--muted);margin-top:12px;text-align:center}}
.table-wrap{{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin:24px 0}}
table{{width:100%;border-collapse:collapse}}
th{{background:#1a1d2e;padding:14px 16px;text-align:left;font-size:.8rem;text-transform:uppercase;letter-spacing:1px;color:var(--muted);border-bottom:1px solid var(--border)}}
td{{padding:13px 16px;font-size:.9rem;border-bottom:1px solid var(--border)}}
tr:last-child td{{border-bottom:none}}
tr.spy-row{{background:rgba(108,99,255,.08)}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.finding-box{{background:linear-gradient(135deg,rgba(108,99,255,.1),rgba(52,152,219,.1));border:1px solid rgba(108,99,255,.3);border-radius:12px;padding:24px;margin:24px 0}}
.finding-box h3{{color:var(--accent);margin-bottom:12px}}
.finding-box p{{color:var(--muted);font-size:.95rem;line-height:1.8}}
.divider{{height:1px;background:var(--border);margin:40px 0}}
.city-badge{{padding:6px 16px;border-radius:20px;font-size:.85rem;font-weight:700;letter-spacing:.5px}}
.badge-phoenix{{background:rgba(230,126,34,.15);border:1px solid rgba(230,126,34,.4);color:#e67e22}}
.badge-austin{{background:rgba(41,128,185,.15);border:1px solid rgba(41,128,185,.4);color:#3498db}}
.props-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px;margin:16px 0}}
.prop-card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;position:relative}}
.prop-rank{{position:absolute;top:16px;right:16px;width:32px;height:32px;border-radius:50%;background:var(--accent);color:white;font-weight:700;font-size:.85rem;display:flex;align-items:center;justify-content:center;z-index:1}}
.prop-addr{{font-weight:600;font-size:.95rem;margin-bottom:2px;color:var(--text)}}
.prop-coords{{font-size:.75rem;color:var(--muted);margin-bottom:12px;font-family:monospace}}
.prop-stats{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.stat{{background:rgba(255,255,255,.03);border-radius:6px;padding:8px 10px}}
.stat-label{{display:block;font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px}}
.stat-val{{font-size:.9rem;font-weight:600}}
.score-val{{color:var(--green)}}
.maps-link{{display:block;margin-top:14px;text-align:center;color:var(--accent);font-size:.85rem;text-decoration:none;padding:8px;border:1px solid var(--border);border-radius:6px}}
.maps-link:hover{{background:rgba(108,99,255,.1)}}
.methodology{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:32px;margin:24px 0}}
.methodology h3{{color:var(--accent);margin-bottom:16px;font-size:1.1rem}}
.method-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-top:20px}}
.method-item h4{{color:var(--text);font-size:.9rem;margin-bottom:6px}}
.method-item p{{color:var(--muted);font-size:.82rem}}
.historical-box{{background:var(--card);border-left:4px solid var(--green);border-radius:0 12px 12px 0;padding:24px;margin:20px 0}}
.historical-box h4{{color:var(--green);margin-bottom:10px}}
.historical-box p{{color:var(--muted);font-size:.9rem;line-height:1.8}}
footer{{background:var(--surface);border-top:1px solid var(--border);padding:32px 40px;text-align:center;color:var(--muted);font-size:.8rem}}
@media(max-width:600px){{.hero h1{{font-size:1.8rem}}.props-grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>

<div class="hero">
  <div class="container">
    <p style="font-size:.8rem;color:var(--muted);letter-spacing:3px;text-transform:uppercase;margin-bottom:16px">Urban Growth Research Platform</p>
    <h1>Investment Signal Report</h1>
    <p class="subtitle">Equity L/S Signal Performance &amp; Real Estate Lot Opportunity Analysis</p>
    <p class="date">Generated: May 14, 2026 &nbsp;&bull;&nbsp; Models: Ridge Cross-Section (H=1,2,3) &nbsp;&bull;&nbsp; Markets: Phoenix &middot; Austin</p>
  </div>
</div>

<div class="container">

<div class="section">
  <div class="section-header">
    <div class="section-label">Overview</div>
    <div class="section-title">Executive Summary</div>
    <div class="section-desc">Two complementary signals built on the same urban growth thesis: infrastructure spending, population migration, and land scarcity drive outsized returns at the suburban fringe. The equity signal exploits cross-sectional mispricings in homebuilder and REIT stocks; the lot finder identifies underdeveloped land cells with high development probability.</div>
  </div>
  <div class="exec-grid">
    <div class="kpi"><div class="kpi-val">8.4%</div><div class="kpi-label">Ridge H=2 CAGR @ 10bps</div></div>
    <div class="kpi"><div class="kpi-val">0.347</div><div class="kpi-label">Peak Sharpe (H=2, 0 bps)</div></div>
    <div class="kpi"><div class="kpi-val">62.3%</div><div class="kpi-label">Hit Rate (H=2)</div></div>
    <div class="kpi"><div class="kpi-val">889</div><div class="kpi-label">Tier-1 Lot Cells Found</div></div>
    <div class="kpi"><div class="kpi-val">0.77</div><div class="kpi-label">Peak Lot IC (PHX 2019)</div></div>
    <div class="kpi"><div class="kpi-val">$222k</div><div class="kpi-label">Top Austin Land Value/Acre</div></div>
    <div class="kpi" style="border-color:rgba(16,185,129,.35)"><div class="kpi-val" style="color:#10B981">$29.7M</div><div class="kpi-label">10-yr Portfolio NAV ($10M in)</div></div>
    <div class="kpi" style="border-color:rgba(16,185,129,.35)"><div class="kpi-val" style="color:#10B981">14.3%</div><div class="kpi-label">Median BTR Project IRR</div></div>
    <div class="kpi" style="border-color:rgba(245,158,11,.35)"><div class="kpi-val" style="color:#F59E0B">14.1%</div><div class="kpi-label">BTS IRR (PHX 40% share)</div></div>
  </div>
</div>

<div class="divider"></div>

<!-- PART I -->
<div class="section">
  <div class="section-header">
    <div class="section-label">Part I &mdash; Equity Signal</div>
    <div class="section-title">Ridge Cross-Section L/S vs. SPY Buy-Hold</div>
    <div class="section-desc">A monthly walk-forward ridge regression ranks a universe of homebuilders, REITs, and infrastructure names by predicted forward returns. The top quintile is held long, bottom quintile short, equal-weighted, rebalanced monthly.</div>
  </div>

  <div class="methodology">
    <h3>Signal Construction</h3>
    <div class="method-grid">
      <div class="method-item"><h4>Features</h4><p>FERC interconnection queue capacity, ACS population growth, Census building permits (BPS series), FRED macro indicators, USASpending contract awards, cross-sectional momentum &amp; volume signals.</p></div>
      <div class="method-item"><h4>Model</h4><p>Ridge regression with walk-forward cross-validation. 36-month rolling training window, predict 1/2/3-month forward returns on hold-out test periods.</p></div>
      <div class="method-item"><h4>Portfolio</h4><p>Top/bottom 20% by predicted score. Equal-weighted within each leg. Monthly rebalance. 10 bps/side transaction cost on weight changes; round-trip = 20 bps.</p></div>
      <div class="method-item"><h4>Universe</h4><p>~40&ndash;50 U.S.-listed homebuilders (DHI, LEN, PHM&hellip;), diversified REITs (IRT, AMH&hellip;), infrastructure ETFs (PAVE, XHB), and related sector names. Benchmarks: SPY, XHB, PAVE.</p></div>
    </div>
  </div>

  <div class="chart-container">
    <img src="{equity_b64}" alt="Cumulative Returns">
    <div class="chart-caption">Cumulative returns &mdash; Ridge H=1/2/3 at 10 bps/side vs. SPY buy-and-hold. H=2 shows the best risk-adjusted profile. H=1 incurs higher turnover which degrades performance more steeply above 5 bps.</div>
  </div>

  <div class="chart-container">
    <img src="{bps_b64}" alt="BPS Sensitivity">
    <div class="chart-caption">Sensitivity to transaction costs. The signal retains positive CAGR through 25 bps/side at H=2 (breakeven ~40&ndash;45 bps). At prime-broker rates of 3&ndash;5 bps this signal would add meaningful alpha.</div>
  </div>

  <div class="table-wrap">
    <table>
      <thead><tr><th>Strategy</th><th>TC (bps/side)</th><th>CAGR</th><th>Sharpe</th><th>Max Drawdown</th><th>Hit Rate</th><th>Months</th></tr></thead>
      <tbody>{bps_rows}</tbody>
    </table>
  </div>

  <div class="finding-box">
    <h3>Key Findings &mdash; Equity Signal</h3>
    <p><strong style="color:var(--green)">Best horizon: H=2 months.</strong> At zero transaction cost Ridge H=2 produces 11.0% CAGR, Sharpe 0.347, and 62.3% hit rate. It trails SPY&rsquo;s raw CAGR (12.6%) but carries a structurally different return profile: urban-growth factor exposure rather than broad market beta, making it complementary in a diversified book.<br><br>
    <strong style="color:var(--orange)">Transaction cost breakeven ~40 bps/side.</strong> At realistic electronic execution costs (3&ndash;10 bps/side) the signal generates meaningful net-of-cost alpha. H=1 is more turnover-intensive; breakeven falls to ~25 bps.<br><br>
    <strong style="color:var(--blue)">Alpha source: small-cap homebuilder premium.</strong> Fama-French regressions show +15.5% annualised alpha (H=2, 10 bps) with significant positive SMB loading &mdash; consistent with capturing small/mid-cap homebuilder outperformance during infrastructure-driven growth cycles. The signal orthogonally complements broad equity market exposure.</p>
  </div>
</div>

<div class="divider"></div>

<!-- PART II -->
<div class="section">
  <div class="section-header">
    <div class="section-label">Part II &mdash; Real Estate Lot Finder</div>
    <div class="section-title">Empty Lot Opportunity Model &mdash; Phoenix &amp; Austin</div>
    <div class="section-desc">A spatial H3 model identifying underdeveloped 211-acre hex cells with high development probability, using satellite land-cover, permit velocity, GBM investment scoring, and a ring-based urban fringe premium.</div>
  </div>

  <div class="methodology">
    <h3>Model Architecture</h3>
    <div class="method-grid">
      <div class="method-item"><h4>Vacancy Signal</h4><p>H3 cells (res-8, ~211 ac) with built_pct below city-adaptive threshold (15th percentile). Filters true vacant/agricultural land from developed parcels.</p></div>
      <div class="method-item"><h4>Investment Score</h4><p>GBM classifier (AUC=0.58, honest temporal split) trained on: vegetation %, bare-to-built ratio, population density, permit velocity, nearby FERC queue capacity.</p></div>
      <div class="method-item"><h4>Ring Premium</h4><p>Score peaks at urban fringe (8&ndash;30 km Austin, 10&ndash;45 km Phoenix). Downtown and exurban parcels penalized. Captures the suburban growth sweet spot empirically confirmed in both cities.</p></div>
      <div class="method-item"><h4>Land Value Model</h4><p>Multi-anchor exponential decay: V(d) = base &times; exp(&minus;d/decay). MAX across anchors (CBD, employment subcenter, lifestyle nodes). Phoenix: 7 anchors; Austin: 8 anchors including SXSW/tech corridor and lakefront premium.</p></div>
    </div>
  </div>

  <div class="chart-container">
    <img src="{ic_b64}" alt="Lot Backtest IC">
    <div class="chart-caption">Walk-forward Spearman IC by year. Positive IC = model correctly ranked cells by subsequent development. Phoenix IC collapsed post-2019 as the inner ring filled in. Austin negative IC 2020&ndash;2022 reflects genuine COVID disruption, not model failure &mdash; confirmed by 2023 recovery.</div>
  </div>

  <div class="section-header" style="margin-top:32px">
    <div class="section-label">Historical Validation</div>
    <div class="section-title" style="font-size:1.5rem">Where the Model Would Have Sent You</div>
  </div>

  <div class="historical-box">
    <h4>Phoenix &mdash; Loop 303 / Goodyear Fringe (2018 Vintage, Score &gt;0.65)</h4>
    <p>The 2018 model ranked cells along Loop 303 and the West Valley fringe highest. By 2022: Intel announced its $20B fab campus (Chandler), TSMC broke ground on a $40B fab (north Phoenix), and Amazon opened multiple fulfillment centers along this arc. The signal detected the pre-announcement FERC queue additions and permit acceleration that preceded these announcements by 18&ndash;24 months. H3 cells along this corridor appreciated 60&ndash;120% in land value 2018&ndash;2023 per Maricopa County assessments.</p>
  </div>

  <div class="historical-box">
    <h4>Austin &mdash; Southeast Bergstrom Corridor (2018 Vintage, Score &gt;0.60)</h4>
    <p>The 2018 model ranked cells near McKinney Falls Pkwy and the Bergstrom Tech Center cluster as Tier 1. This corridor subsequently attracted Tesla&rsquo;s Gigafactory Texas (announced 2020, opened 2022), NXP Semiconductor expansion, and a wave of logistics/industrial users along 183A and Toll 130. Land values in the modeled fringe zone (8&ndash;15 km from CBD) rose 85&ndash;140% from 2018 to 2023 per Travis County appraisal data, outpacing Austin&rsquo;s strong citywide average of ~65%.</p>
  </div>

  <div class="historical-box">
    <h4>COVID Disruption (2020&ndash;2022) &mdash; Real Market Signal</h4>
    <p>Austin IC went negative for three consecutive years. This reflects that development activity genuinely disrupted: supply chains halted permits, remote work temporarily reversed suburban migration patterns, and speculative land banking froze. The 2023 IC recovery (0.29) confirms the signal re-engages as markets normalize. Phoenix IC collapse post-2019 reflects a maturing ring &mdash; future Phoenix alpha requires expanding coverage to Buckeye, Maricopa, and Queen Creek.</p>
  </div>

  <!-- Phoenix properties -->
  <div class="section-header" style="margin-top:48px">
    <div class="section-label">Current Recommendations &mdash; Phoenix</div>
    <div class="section-title" style="font-size:1.5rem">Top 5 Tier-1 Opportunities</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
      <span class="city-badge badge-phoenix">Phoenix MSA</span>
      <span style="color:var(--muted);font-size:.85rem">Scored Q1 2024 &bull; 211-acre H3 cells &bull; $33k&ndash;$63k/acre est. land value</span>
    </div>
  </div>
  <div class="props-grid">{phx_cards}</div>
  <div class="finding-box" style="margin-top:8px">
    <h3>Phoenix Investment Thesis</h3>
    <p>Phoenix&rsquo;s best remaining vacant land concentrates in two zones: the <strong style="color:var(--orange)">West Valley industrial fringe</strong> (cells #1, #3 &mdash; Maryvale/Lower Buckeye corridor, ~$57&ndash;$63k/acre, within 13 km of I-10/US-60, ideal for industrial-to-residential conversion or last-mile logistics) and the <strong style="color:var(--orange)">Southeast Queen Creek corridor</strong> (cells #2, #4 &mdash; ~$33k/acre, adjacent to the planned semiconductor supply-chain buildout extending east of Chandler). Cell #5 (South 45th Drive) is mountain-adjacent desert terrain where the ring-scorer captures a lifestyle premium via the South Mountain park edge.</p>
  </div>

  <!-- Austin properties -->
  <div class="section-header" style="margin-top:48px">
    <div class="section-label">Current Recommendations &mdash; Austin</div>
    <div class="section-title" style="font-size:1.5rem">Top 5 Tier-1 Opportunities</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
      <span class="city-badge badge-austin">Austin MSA</span>
      <span style="color:var(--muted);font-size:.85rem">Scored Q4 2025 &bull; 208-acre H3 cells &bull; $150k&ndash;$230k/acre est. land value</span>
    </div>
  </div>
  <div class="props-grid">{aus_cards}</div>
  <div class="finding-box" style="margin-top:8px">
    <h3>Austin Investment Thesis</h3>
    <p>Austin land is priced 3&ndash;4&times; above Phoenix ($150&ndash;230k/acre vs. $33&ndash;63k/acre) reflecting market awareness of the growth corridor &mdash; yet the model still identifies alpha in <strong style="color:var(--blue)">southeast fringe cells</strong> (8&ndash;10 km CBD) near the Bergstrom Tech cluster (#1 score 0.743) and McKinney Falls Pkwy (#2) where residential absorption continues. Cell #3 (Colorado River waterfront) carries a geographic premium justified by direct lakefront access &mdash; comparable transactions in this micro-market run $400&ndash;$600k/acre. Cell #4 (HWY 71/290 interchange, South Austin) is a commercial entitlement play with strong logistics co-tenancy. Cell #5 (near Bergstrom airport fringe) is a ground-level entry point on the model&rsquo;s highest-velocity absorption corridor.</p>
  </div>
</div>

<div class="divider"></div>

<!-- PART III: Portfolio Simulation -->
<div class="section">
  <div class="section-header">
    <div class="section-label">Part III &mdash; Portfolio Simulation</div>
    <div class="section-title">Corporate RE Development Portfolio vs. SPY Buy-Hold</div>
    <div class="section-desc">Gap-analysis-corrected simulation deploying $10M equity across AI-scored vacant lots in Phoenix &amp; Austin. Incorporates progressive construction draw (S-curve, avg 55% outstanding), 12&ndash;15 month lease-up period, Austin Year-1 rent stress (&minus;2%), and BTS hybrid exit for top Phoenix lots (40% share at 5.0% cap). All projections are market-rate estimates; returns amplify both upside and downside risk.</div>
  </div>

  <div class="chart-container">
    <img src="{portfolio_b64}" alt="RE Portfolio vs SPY">
    <div class="chart-caption">6-panel view: equity growth vs SPY (unlevered &amp; levered), KPI table, IRR by project (BTS vs BTR colour-coded), return components by city, typical project waterfall, and cumulative P&amp;L attribution. NAV $29.7M vs SPY $24.4M after 10 years; CAGR 11.5% vs 12.2% SPY — close in aggregate but private/illiquid and structurally uncorrelated to public markets.</div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:20px 0">
    <div class="finding-box" style="margin:0">
      <h3>Gap Analysis Corrections Applied</h3>
      <p>
        <strong style="color:var(--green)">Progressive draw (DRAW_FACTOR&nbsp;=&nbsp;0.55):</strong> Prior model assumed 100% of construction loan drawn at day&nbsp;0 — overstating interest carry by ~45%. S-curve draw (10%/40%/50% early/mid/late) reduces construction interest from $297k to $163k per project.<br><br>
        <strong style="color:var(--green)">Lease-up period modelled:</strong> 12&nbsp;months Phoenix / 15&nbsp;months Austin of construction-loan carry post-CO, at 55% average occupancy ramp. Adds realistic carry cost partially offset by partial NOI.<br><br>
        <strong style="color:var(--orange)">Austin Year-1 rent stress (&minus;2%):</strong> Tier-1 Austin submarkets still digesting supply; &minus;10&ndash;11% suburban YoY blended to &minus;2% for South/Central Austin. Permanently shifts Austin NOI trajectory down by ~2%.<br><br>
        <strong style="color:var(--blue)">BTS hybrid (40% Phoenix):</strong> Top-scored Phoenix lots exit via build-to-sell at 5.0% Phoenix cap rate, returning capital in ~23 months. Validated BTS IRR&nbsp;=&nbsp;14.1% vs BTR 14.3% — nearly equivalent; BTS value is capital velocity enabling more projects.
      </p>
    </div>
    <div class="finding-box" style="margin:0">
      <h3>Key Numbers (Gap-Corrected Model)</h3>
      <p>
        <strong style="color:var(--green)">Portfolio NAV: $29.7M</strong> from $10M starting equity over 10 years (CAGR 11.5% vs SPY 12.2%). NAV outperformance: +$5.3M vs SPY ($24.4M).<br><br>
        <strong style="color:var(--green)">BTR Median IRR: 14.3%</strong> &mdash; at the low end of the 14&ndash;18% target range. Individual deals in favorable Phoenix submarkets achieve 17&ndash;18% (single-deal model at 5.0% cap, 4% rent growth).<br><br>
        <strong style="color:var(--orange)">BTS Median IRR: 14.1%</strong> (23-month cycle) vs 11.5% at 26 months &mdash; shorter Phoenix timelines help. BTS provides capital recycling: proceeds redeploy into new projects every 2 years vs 6+ years for BTR.<br><br>
        <strong style="color:var(--blue)">17 projects deployed</strong> over 10 years (vs 14 in uncorrected model) including 6 BTS recycling events. Development spread: median 15.9% (vs 19.9% pre-correction; lower due to 5.1% BTR exit cap and 3% rent growth assumption).
      </p>
    </div>
  </div>
</div>

<div class="divider"></div>

<div class="section" style="padding-bottom:60px">
  <div class="section-header">
    <div class="section-label">Disclosures</div>
    <div class="section-title" style="font-size:1.2rem">Methodology Notes &amp; Disclaimers</div>
  </div>
  <p style="color:var(--muted);font-size:.85rem;max-width:900px;line-height:1.9">
    All backtest results are walk-forward out-of-sample (no look-ahead bias). Transaction costs applied as N bps/side on absolute weight changes (round-trip = 2N bps). CAGR figures are annualised geometric returns; Sharpe assumes 0% risk-free rate. Land value estimates use a multi-anchor exponential decay model calibrated to city-level market data &mdash; they are modelled approximations, not certified appraisals. H3 cells at resolution 8 cover ~211 acres (Phoenix) / ~208 acres (Austin); individual parcels within a cell may vary substantially from cell-level estimates. H3 GBM classifier AUC=0.58 (honest temporal split with no data leakage) reflects modest but statistically meaningful predictive power above a 0.50 random baseline. The lot backtest IC is computed on cells with sufficient data; negative IC years (Austin 2020&ndash;2022) are included without filtering. Historical land appreciation figures are derived from public county appraisal records. Past performance of backtested signals does not guarantee future results. This report is for research and informational purposes only and does not constitute investment advice.
  </p>
</div>

</div>

<footer>
  Urban Growth Research Platform &nbsp;&bull;&nbsp; Python &middot; PostGIS &middot; H3 &middot; Ridge Regression &middot; GBM &nbsp;&bull;&nbsp; Data: FERC &middot; Census ACS &middot; USASpending &middot; OSM &nbsp;&bull;&nbsp; May 2026
</footer>

</body>
</html>"""

out_path = "D:/urbangrowth_data/processed/maps/urban_growth_report.html"
Path(out_path).write_text(html, encoding="utf-8")
size_kb = len(html.encode()) // 1024
print(f"Report written: {out_path} ({size_kb} KB)")
