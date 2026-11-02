"""從 data/foodsec.db 算指標，輸出 docs/index.html（自包含，資料內嵌）與 docs/data.json。

用法：py -3.14 -I build_dashboard.py [--db data/foodsec.db] [--out docs] [--chart-days 180]
輸出到 docs/ 是為了 GitHub Pages 直接從 main 分支的 docs 資料夾發佈。

五個視角（左側按鈕切換，同一份資料不同呈現）：
    採購：本週買菜參考（偏貴／正常／偏便宜）加全表
    農民：供應警示、交易量變化、去年同期，表格依量變化排序
    媒體：把異常變成可引用的句子
    政府與農會：供應吃緊、供過於求、整體交易量指數、第二階段資料佔位
    我要種什麼：家用水耕可種的作物，現價等級加預計採收日的去年同期價

指標（每個監測作物，品種合併、排除名稱含「進口」者，全台各市場以交易量加權）：
    最新交易日均價、近 7 天均價、近 30 天均價、近 90 天均價與標準差、去年同期（365±7 天）均價
    價格變動：近 7 天 vs 近 30 天、近 7 天 vs 去年同期；交易量變動：近 7 天日均量 vs 近 30 天日均量
    z 分數：(近 7 天均價 − 近 90 天均價) / 近 90 天標準差，近 90 天交易日少於 20 天不算
買菜等級：近 7 天 vs 近 30 天 ≥ +15% 或 z ≥ 1 為「偏貴」；≤ −15% 或 z ≤ −1 為「偏便宜」；其餘「正常」；資料未滿 30 天不分類。
供應警示：|z| ≥ 2 或 近 7 天 vs 近 30 天變動 ≥ 30%。
watch_crops.txt 格式：主名|顯示名|群組|家用水耕生長天數（空＝不適合家用機）。
表格每欄可點標題排序。圖表用 Chart.js（cdnjs 固定版本），沒有網路時表格仍可看。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sqlite3
import statistics
import string
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHART_JS = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"


def load_watch(path: str) -> list[dict]:
    items = []
    if not os.path.exists(path):
        return items
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if not parts or not parts[0]:
                continue
            hydro = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            field = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
            season = parts[5] if len(parts) > 5 else ""
            items.append({"key": parts[0], "name": parts[1] if len(parts) > 1 and parts[1] else parts[0],
                          "group": parts[2] if len(parts) > 2 and parts[2] else "其他",
                          "hydro_days": hydro, "field_days": field, "season": season})
    return items


def season_ok(season: str, month: int) -> bool | None:
    """適種月份字串：全年／9-3（跨年）／3-5,9-11。回 None 表示沒填。"""
    s = (season or "").strip()
    if not s:
        return None
    if s == "全年":
        return True
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                a, b = int(a), int(b)
            except ValueError:
                continue
            months = list(range(a, b + 1)) if a <= b else list(range(a, 13)) + list(range(1, b + 1))
            if month in months:
                return True
        elif part.isdigit() and int(part) == month:
            return True
    return False


def daily_series(con: sqlite3.Connection, key: str) -> list[tuple[str, float, float]]:
    """回 [(date, 加權均價, 總量)]，品種合併、排除進口。"""
    rows = con.execute(
        """SELECT TRANS_DATE,
                  SUM(AVG_PRICE * TRANS_QTY) / NULLIF(SUM(TRANS_QTY), 0) AS WPRICE,
                  SUM(TRANS_QTY) AS QTY
           FROM PRICE_DAILY
           WHERE (CROP_NAME = ? OR CROP_NAME LIKE ?)
             AND CROP_NAME NOT LIKE '%進口%'
             AND AVG_PRICE IS NOT NULL AND TRANS_QTY IS NOT NULL AND TRANS_QTY > 0
           GROUP BY TRANS_DATE
           HAVING QTY > 0
           ORDER BY TRANS_DATE""",
        (key, key + "-%"),
    ).fetchall()
    return [(d, float(p), float(q)) for d, p, q in rows if p is not None]


def mean_or_none(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 1) if xs else None


def window(series: list[tuple[str, float, float]], latest: dt.date, days: int) -> list[tuple[str, float, float]]:
    lo = latest - dt.timedelta(days=days - 1)
    return [r for r in series if lo <= dt.date.fromisoformat(r[0]) <= latest]


def around_last_year(series: list[tuple[str, float, float]], target: dt.date) -> list[tuple[str, float, float]]:
    lo, hi = target - dt.timedelta(days=372), target - dt.timedelta(days=358)
    return [r for r in series if lo <= dt.date.fromisoformat(r[0]) <= hi]


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100, 1)


def analyze(series: list[tuple[str, float, float]], chart_days: int, w: dict) -> dict | None:
    if not series:
        return None
    latest = dt.date.fromisoformat(series[-1][0])
    w7, w30, w90 = window(series, latest, 7), window(series, latest, 30), window(series, latest, 90)
    wly = around_last_year(series, latest)

    p_latest = round(series[-1][1], 1)
    p7, p30, p90 = mean_or_none([r[1] for r in w7]), mean_or_none([r[1] for r in w30]), mean_or_none([r[1] for r in w90])
    p_ly = mean_or_none([r[1] for r in wly])
    q7, q30 = mean_or_none([r[2] for r in w7]), mean_or_none([r[2] for r in w30])
    q_ly = mean_or_none([r[2] for r in wly])

    z = None
    if len(w90) >= 20 and p7 is not None:
        sd = statistics.pstdev([r[1] for r in w90])
        if sd > 0:
            z = round((p7 - statistics.fmean([r[1] for r in w90])) / sd, 2)

    chg_30 = pct(p7, p30)
    chg_ly = pct(p7, p_ly)
    chg_qty = pct(q7, q30)
    chg_qty_ly = pct(q7, q_ly)
    enough_30 = len(w30) >= 15

    alert = None
    if z is not None and z >= 2:
        alert = "價格偏高"
    elif z is not None and z <= -2:
        alert = "價格偏低"
    elif enough_30 and chg_30 is not None and chg_30 >= 30:
        alert = "近週大漲"
    elif enough_30 and chg_30 is not None and chg_30 <= -30:
        alert = "近週大跌"

    buy = None
    if (z is not None and z >= 1) or (enough_30 and chg_30 is not None and chg_30 >= 15):
        buy = "偏貴"
    elif (z is not None and z <= -1) or (enough_30 and chg_30 is not None and chg_30 <= -15):
        buy = "偏便宜"
    elif enough_30 and chg_30 is not None:
        buy = "正常"

    def plan(days: int | None, season: str) -> dict | None:
        if not days:
            return None
        target = latest + dt.timedelta(days=days)
        p_h = mean_or_none([r[1] for r in around_last_year(series, target)])
        in_season = season_ok(season, latest.month)
        if in_season is False:
            hint = "現在不是適種月份"
        elif buy == "偏貴" and p_h is not None and p30 is not None and p_h >= p30 * 0.9:
            hint = "值得種：現在貴，去年到採收期也不便宜"
        elif buy == "偏貴" and p_h is not None and p30 is not None:
            hint = "小心：現在貴，但去年採收期那時已回跌"
        elif buy == "偏貴":
            hint = "現在貴，缺去年資料看不出採收期走勢"
        elif buy == "偏便宜":
            hint = "現在便宜，為了省錢種不划算"
        elif buy == "正常":
            hint = "價格正常，種來吃新鮮可以"
        else:
            hint = "資料未滿 30 天"
        return {"days": days, "harvest_date": target.isoformat(), "price_harvest_ly": p_h,
                "season": season, "in_season": in_season, "hint": hint}

    grow = {"hydro": plan(w.get("hydro_days"), ""), "field": plan(w.get("field_days"), w.get("season") or "")}

    chart = window(series, latest, chart_days)
    return {
        "latest_date": series[-1][0], "price_latest": p_latest,
        "price_7d": p7, "price_30d": p30, "price_90d": p90, "price_ly": p_ly,
        "chg_vs_30d": chg_30, "chg_vs_ly": chg_ly, "z90": z,
        "qty_7d": q7, "qty_30d": q30, "qty_ly": q_ly, "chg_qty": chg_qty, "chg_qty_ly": chg_qty_ly,
        "days_30": len(w30), "days_90": len(w90), "alert": alert, "buy": buy,
        "grow": grow,
        "chart": {"dates": [r[0] for r in chart], "prices": [round(r[1], 1) for r in chart], "qty": [round(r[2]) for r in chart]},
    }


def qty_index(series_map: dict[str, list[tuple[str, float, float]]]) -> dict:
    """所有監測作物每日總交易量，算近 7 天、近 30 天、去年同期的日均。"""
    totals: dict[str, float] = {}
    for s in series_map.values():
        for d, _p, q in s:
            totals[d] = totals.get(d, 0.0) + q
    if not totals:
        return {"q7": None, "q30": None, "q_ly": None, "chg_30": None, "chg_ly": None}
    series = [(d, 0.0, q) for d, q in sorted(totals.items())]
    latest = dt.date.fromisoformat(series[-1][0])
    q7 = mean_or_none([r[2] for r in window(series, latest, 7)])
    q30 = mean_or_none([r[2] for r in window(series, latest, 30)])
    q_ly = mean_or_none([r[2] for r in around_last_year(series, latest)])
    return {"q7": q7, "q30": q30, "q_ly": q_ly, "chg_30": pct(q7, q30), "chg_ly": pct(q7, q_ly)}


def fmt(v, suffix: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}{suffix}" if isinstance(v, (int, float)) else str(v)


def fmt_pct(v) -> str:
    if v is None:
        return "-"
    sign = "+" if v > 0 else ""
    return f"{sign}{v}%"


def word_pct(v) -> str:
    if v is None:
        return "無法比較"
    if v > 0:
        return f"上漲 {v}%"
    if v < 0:
        return f"下跌 {abs(v)}%"
    return "持平"


def dv(v) -> str:
    """排序用的 data-v 屬性值。"""
    return "" if v is None else html.escape(str(v))


def level_class(level: str | None) -> str:
    return {"偏貴": "hi", "偏便宜": "lo", "正常": "ok"}.get(level or "", "")


def alert_class(a: str | None) -> str:
    if a in ("價格偏高", "近週大漲"):
        return "hi"
    if a in ("價格偏低", "近週大跌"):
        return "lo"
    return ""


TEMPLATE = string.Template(r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>糧食供應監測</title>
<style>
:root { --bg:#f6f8f6; --card:#fff; --text:#212529; --muted:#6c757d; --green:#2e7d32; --hi:#fff3e0; --hi-t:#e65100; --lo:#e3f2fd; --lo-t:#0d47a1; --ok:#e8f5e9; --ok-t:#1b5e20; --line:#e3e6e3; }
@media (prefers-color-scheme: dark) { :root { --bg:#141814; --card:#1e241e; --text:#e8ebe8; --muted:#9aa39a; --hi:#3a2a14; --hi-t:#ffb74d; --lo:#14253a; --lo-t:#90caf9; --ok:#1c2e1c; --ok-t:#a5d6a7; --line:#2e362e; } }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif; background:var(--bg); color:var(--text); }
header { background:var(--green); color:#fff; padding:.8rem 1rem; }
header h1 { margin:0; font-size:1.2rem; }
header .sub { font-size:.85rem; opacity:.9; }
.layout { display:flex; gap:1rem; max-width:1280px; margin:0 auto; padding:1rem; align-items:flex-start; }
nav.side { flex:0 0 190px; position:sticky; top:1rem; display:flex; flex-direction:column; gap:.4rem; }
nav.side button { text-align:left; background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:10px; padding:.6rem .8rem; font-size:.95rem; cursor:pointer; }
nav.side button:hover { border-color:var(--green); }
nav.side button.active { background:var(--green); color:#fff; border-color:var(--green); font-weight:700; }
nav.side .who { font-size:.75rem; color:var(--muted); padding:.2rem .3rem 0; }
main { flex:1; min-width:0; }
section[data-view] { display:none; }
section[data-view].on { display:block; }
h2 { font-size:1.05rem; margin:1.2rem 0 .6rem; }
h2:first-child { margin-top:0; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.6rem; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.7rem .9rem; }
.stat .v { font-size:1.5rem; font-weight:700; color:var(--green); }
.stat .l { font-size:.8rem; color:var(--muted); }
.cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:.6rem; }
.cols .col { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.7rem .9rem; }
.cols .col h3 { margin:0 0 .5rem; font-size:.95rem; }
.cols .col.hi h3 { color:var(--hi-t); } .cols .col.lo h3 { color:var(--lo-t); } .cols .col.ok h3 { color:var(--ok-t); }
.cols ul { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.25rem; font-size:.9rem; }
.cols li { display:flex; justify-content:space-between; gap:.5rem; }
.cols li span:last-child { color:var(--muted); font-variant-numeric:tabular-nums; text-align:right; }
ul.alerts, ul.news { list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:.4rem; }
ul.alerts li, ul.news li { background:var(--card); border:1px solid var(--line); border-left:5px solid var(--muted); border-radius:10px; padding:.55rem .8rem; }
ul.alerts li.hi, ul.news li.hi { border-left-color:var(--hi-t); }
ul.alerts li.lo, ul.news li.lo { border-left-color:var(--lo-t); }
ul.news li { line-height:1.6; }
ul.news li .src { display:block; font-size:.75rem; color:var(--muted); }
.table-wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:12px; }
table { border-collapse:collapse; width:100%; font-size:.85rem; white-space:nowrap; }
th, td { padding:.45rem .6rem; border-bottom:1px solid var(--line); text-align:right; font-variant-numeric:tabular-nums; }
th:first-child, td:first-child { text-align:left; }
th { position:sticky; top:0; background:var(--card); cursor:pointer; user-select:none; }
th:hover { color:var(--green); }
th.asc::after { content:" ▲"; font-size:.7em; } th.desc::after { content:" ▼"; font-size:.7em; }
tr.hi td { background:var(--hi); }
tr.lo td { background:var(--lo); }
td.txt { text-align:left; white-space:normal; min-width:14rem; }
.grp { font-size:.7rem; color:var(--muted); margin-left:.4rem; }
.tag { display:inline-block; padding:.05rem .45rem; border-radius:999px; font-size:.75rem; }
.tag.hi { background:var(--hi); color:var(--hi-t); } .tag.lo { background:var(--lo); color:var(--lo-t); } .tag.ok { background:var(--ok); color:var(--ok-t); }
.muted { color:var(--muted); }
.hint { font-size:.8rem; color:var(--muted); margin:.2rem 0 .6rem; line-height:1.5; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:.8rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.7rem .9rem; }
.card-head { display:flex; justify-content:space-between; align-items:baseline; gap:.5rem; margin-bottom:.4rem; font-size:.9rem; }
.todo { background:var(--card); border:1px dashed var(--line); border-radius:12px; padding:.7rem .9rem; font-size:.85rem; color:var(--muted); }
footer { font-size:.78rem; color:var(--muted); margin:2rem 0 1rem; line-height:1.6; }
@media (max-width: 760px) {
  .layout { flex-direction:column; }
  nav.side { position:static; flex-direction:row; flex-wrap:wrap; width:100%; }
  nav.side button { flex:1 1 auto; font-size:.85rem; padding:.45rem .6rem; }
  nav.side .who { display:none; }
}
</style>
</head>
<body>
<header>
  <h1>糧食供應監測：批發市場價格</h1>
  <div class="sub">資料 $date_min ~ $date_max，$markets 個市場，產生於 $generated_at</div>
</header>
<div class="layout">
<nav class="side">
  <div class="who">我是誰</div>
  <button data-view="buy">🛒 家庭與餐飲採購</button>
  <button data-view="farm">🌾 農民產銷</button>
  <button data-view="media">📰 媒體：有什麼新聞</button>
  <button data-view="gov">🏛️ 政府與農會</button>
  <button data-view="grow">🌱 我要種什麼</button>
</nav>
<main>
  <section data-view="buy">
    <h2>本週買菜參考</h2>
    <div class="hint">近 7 天均價對近 30 天：高 15% 以上算偏貴，低 15% 以上算偏便宜；z 分數 ±1 也會歸類。資料不滿 30 天的作物先不分類。批發價不等於零售價，但方向一致、通常早幾天反映。</div>
    <div class="cols">
      <div class="col hi"><h3>偏貴，先別買</h3><ul>$buy_hi</ul></div>
      <div class="col ok"><h3>正常</h3><ul>$buy_ok</ul></div>
      <div class="col lo"><h3>偏便宜，可多買</h3><ul>$buy_lo</ul></div>
    </div>
  </section>

  <section data-view="farm">
    <h2>供應警示</h2>
    <div class="hint">|z| ≥ 2 或近 7 天 vs 近 30 天變動 ≥ 30%。價格偏高通常是供應吃緊，偏低是供過於求；配合交易量看：量縮價漲是減產，量增價跌是盛產。</div>
    <ul class="alerts">$alerts</ul>
    <h2>交易量變化</h2>
    <div class="cols">
      <div class="col lo"><h3>量縮最多（近 7 天 vs 近 30 天）</h3><ul>$qty_down</ul></div>
      <div class="col hi"><h3>量增最多</h3><ul>$qty_up</ul></div>
    </div>
  </section>

  <section data-view="media">
    <h2>新聞線索</h2>
    <div class="hint">依變動幅度排序，數字可直接引用，來源與資料日期附在每條後面。</div>
    <ul class="news">$news</ul>
  </section>

  <section data-view="gov">
    <h2>整體供應</h2>
    <div class="stats">
      <div class="stat"><div class="v">$qi_q7</div><div class="l">監測作物近 7 天日均交易量（公斤）</div></div>
      <div class="stat"><div class="v">$qi_chg30</div><div class="l">對近 30 天</div></div>
      <div class="stat"><div class="v">$qi_chgly</div><div class="l">對去年同期</div></div>
      <div class="stat"><div class="v">$alert_count</div><div class="l">供應警示</div></div>
    </div>
    <h2>需要注意的品項</h2>
    <div class="cols">
      <div class="col hi"><h3>供應吃緊（價高且較去年同期高）</h3><ul>$gov_tight</ul></div>
      <div class="col lo"><h3>供過於求（價低，可能需要調節）</h3><ul>$gov_glut</ul></div>
    </div>
    <h2>第二階段要接的資料</h2>
    <div class="todo">自給率（糧食供需年報）、黃豆小麥玉米進口量與來源國（農產貿易統計）、氣象觀測與警報。前兩項沒有 API，手動 CSV 範本在 input/；接上後這裡會顯示進口依存度與氣候風險。</div>
  </section>

  <section data-view="grow">
    <h2>我要種什麼</h2>
    <div class="hint">「現在貴」不代表採收時還貴，所以附上去年同期在預計採收日的價格當參考；去年資料要累積一年才有。天數與適種月份是常見值，以花蓮平地為準，待自種核對。</div>
    <h2>露地（有田）</h2>
    <div class="table-wrap">
    <table id="tbl-grow-f">
      <thead><tr>
        <th data-key="name" data-text="1">作物</th>
        <th data-key="buy" data-text="1">現價等級</th>
        <th data-key="p7">近 7 天</th>
        <th data-key="chg30">vs 30 天</th>
        <th data-key="days">天數</th>
        <th data-key="season" data-text="1">適種月份</th>
        <th data-key="now" data-text="1">現在</th>
        <th data-key="harvest" data-text="1">預計採收</th>
        <th data-key="phly">去年採收期價</th>
        <th data-key="hint" data-text="1">建議</th>
      </tr></thead>
      <tbody>$grow_rows_f</tbody>
    </table>
    </div>
    <h2>家用水耕機</h2>
    <div class="table-wrap">
    <table id="tbl-grow-h">
      <thead><tr>
        <th data-key="name" data-text="1">作物</th>
        <th data-key="buy" data-text="1">現價等級</th>
        <th data-key="p7">近 7 天</th>
        <th data-key="chg30">vs 30 天</th>
        <th data-key="days">天數</th>
        <th data-key="harvest" data-text="1">預計採收</th>
        <th data-key="phly">去年採收期價</th>
        <th data-key="hint" data-text="1">建議</th>
      </tr></thead>
      <tbody>$grow_rows_h</tbody>
    </table>
    </div>
  </section>

  <section data-view="buy farm gov" class="shared">
    <h2>監測作物指標（元/公斤，全台交易量加權）</h2>
    <div class="hint">點欄位標題排序，再點一次反向。採購視角預設依「vs 30 天」，農民視角預設依「量 vs 30 天」。</div>
    <div class="table-wrap">
    <table id="tbl">
      <thead><tr>
        <th data-key="name" data-text="1">作物</th>
        <th data-key="buy" data-text="1">等級</th>
        <th data-key="latest">最新</th>
        <th data-key="p7">近 7 天</th>
        <th data-key="p30">近 30 天</th>
        <th data-key="chg30">vs 30 天</th>
        <th data-key="ply">去年同期</th>
        <th data-key="chgly">vs 去年</th>
        <th data-key="z">z（90 天）</th>
        <th data-key="chgqty">量 vs 30 天</th>
        <th data-key="alert" data-text="1">警示</th>
      </tr></thead>
      <tbody>$rows</tbody>
    </table>
    </div>
    <h2>近 $chart_days 天走勢（線＝均價，柱＝交易量）</h2>
    <div class="cards">$cards</div>
  </section>

  <footer>
    資料來源：農業部資料開放平台「農產品交易行情」（AgriProductsTransType），批發市場每日行情，免金鑰 API。<br>
    計算：品種合併、排除名稱含「進口」者；日均價＝各市場均價以交易量加權。z＝(近 7 天均價 − 近 90 天均價) ÷ 近 90 天標準差，近 90 天交易日不足 20 天不算。去年同期＝往前 365 ± 7 天。<br>
    這是價格面的供應訊號，不是糧食安全的全貌；自給率、進口依存度、庫存要另外接年報與貿易統計。
  </footer>
</main>
</div>
<script src="$chart_js"></script>
<script>
const DATA = $data_json;
var DEFAULT_SORT = { buy: 'chg30', farm: 'chgqty', gov: 'chg30' };
function makeSortable(table) {
  if (!table) return null;
  var ths = Array.prototype.slice.call(table.querySelectorAll('th[data-key]'));
  var current = { idx: -1, dir: -1 };
  function val(td) {
    var v = td.getAttribute('data-v');
    if (v === null || v === '') return null;
    var n = Number(v);
    return isNaN(n) ? v : n;
  }
  function sortBy(idx, dir) {
    var tbody = table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var ca = a.cells[idx], cb = b.cells[idx];
      var va = ca ? val(ca) : null, vb = cb ? val(cb) : null;
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (typeof va === 'string' || typeof vb === 'string') return String(va).localeCompare(String(vb), 'zh-Hant') * dir;
      return (va - vb) * dir;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    ths.forEach(function (t) { t.classList.remove('asc', 'desc'); });
    ths[idx].classList.add(dir > 0 ? 'asc' : 'desc');
    current = { idx: idx, dir: dir };
  }
  ths.forEach(function (th, idx) {
    th.addEventListener('click', function () {
      var dir = current.idx === idx ? -current.dir : (th.getAttribute('data-text') ? 1 : -1);
      sortBy(idx, dir);
    });
  });
  return {
    byKey: function (key, dir) {
      var idx = ths.findIndex(function (t) { return t.getAttribute('data-key') === key; });
      if (idx >= 0) sortBy(idx, dir === undefined ? -1 : dir);
    }
  };
}
var mainSorter = makeSortable(document.getElementById('tbl'));
['tbl-grow-f', 'tbl-grow-h'].forEach(function (id) {
  var s = makeSortable(document.getElementById(id));
  if (s) s.byKey('chg30', -1);
});
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll('nav.side button[data-view]'));
  var sections = Array.prototype.slice.call(document.querySelectorAll('section[data-view]'));
  function show(view) {
    buttons.forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-view') === view); });
    sections.forEach(function (s) {
      var views = s.getAttribute('data-view').split(' ');
      s.classList.toggle('on', views.indexOf(view) >= 0);
    });
    if (mainSorter && DEFAULT_SORT[view]) mainSorter.byKey(DEFAULT_SORT[view], -1);
    try { localStorage.setItem('foodsec.view', view); } catch (e) { }
    if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  }
  buttons.forEach(function (b) { b.addEventListener('click', function () { show(b.getAttribute('data-view')); }); });
  var initial = location.hash.replace('#', '');
  if (!initial) { try { initial = localStorage.getItem('foodsec.view') || ''; } catch (e) { } }
  if (!buttons.some(function (b) { return b.getAttribute('data-view') === initial; })) initial = 'buy';
  show(initial);
})();
(function () {
  if (typeof Chart === 'undefined') return;
  var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  var grid = dark ? '#2e362e' : '#e3e6e3';
  var txt = dark ? '#9aa39a' : '#6c757d';
  DATA.crops.forEach(function (c) {
    if (!c.m) return;
    var el = document.getElementById('c-' + c.key);
    if (!el) return;
    new Chart(el, {
      data: {
        labels: c.m.chart.dates,
        datasets: [
          { type: 'line', label: '均價', data: c.m.chart.prices, borderColor: '#2e7d32', backgroundColor: '#2e7d32', pointRadius: 0, borderWidth: 2, tension: .2, yAxisID: 'y' },
          { type: 'bar', label: '交易量', data: c.m.chart.qty, backgroundColor: dark ? 'rgba(144,202,249,.35)' : 'rgba(13,71,161,.18)', yAxisID: 'y2' }
        ]
      },
      options: {
        responsive: true, animation: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: txt, maxTicksLimit: 6, maxRotation: 0 }, grid: { color: grid } },
          y: { position: 'left', ticks: { color: txt }, grid: { color: grid }, title: { display: true, text: '元/公斤', color: txt } },
          y2: { position: 'right', ticks: { color: txt }, grid: { drawOnChartArea: false }, title: { display: true, text: '公斤', color: txt } }
        }
      }
    });
  });
})();
</script>
</body>
</html>
""")


def render_html(data: dict) -> str:
    s = data["summary"]
    qi = data["qty_index"]
    crops = data["crops"]
    ordered = sorted(crops, key=lambda x: (x["group"], x["name"]))
    with_m = [c for c in crops if c["m"]]

    def name_cell(c: dict) -> str:
        return f"<td data-v='{html.escape(c['name'])}'>{html.escape(c['name'])}<span class='grp'>{html.escape(c['group'])}</span></td>"

    def tag(level: str | None) -> str:
        return f"<span class='tag {level_class(level)}'>{html.escape(level)}</span>" if level else "<span class='muted'>-</span>"

    rows = []
    for c in ordered:
        m = c["m"]
        if not m:
            rows.append(f"<tr>{name_cell(c)}<td colspan='10' class='muted'>無資料（主名「{html.escape(c['key'])}」在資料庫找不到）</td></tr>")
            continue
        rows.append(
            f"<tr class='{alert_class(m['alert'])}'>{name_cell(c)}"
            f"<td data-v='{dv(m['buy'])}'>{tag(m['buy'])}</td>"
            f"<td data-v='{dv(m['price_latest'])}'>{fmt(m['price_latest'])}</td>"
            f"<td data-v='{dv(m['price_7d'])}'>{fmt(m['price_7d'])}</td>"
            f"<td data-v='{dv(m['price_30d'])}'>{fmt(m['price_30d'])}</td>"
            f"<td data-v='{dv(m['chg_vs_30d'])}'>{fmt_pct(m['chg_vs_30d'])}</td>"
            f"<td data-v='{dv(m['price_ly'])}'>{fmt(m['price_ly'])}</td>"
            f"<td data-v='{dv(m['chg_vs_ly'])}'>{fmt_pct(m['chg_vs_ly'])}</td>"
            f"<td data-v='{dv(m['z90'])}'>{fmt(m['z90'])}</td>"
            f"<td data-v='{dv(m['chg_qty'])}'>{fmt_pct(m['chg_qty'])}</td>"
            f"<td data-v='{dv(m['alert'])}'>{html.escape(m['alert'] or '')}</td></tr>"
        )

    def buy_list(level: str, reverse: bool) -> str:
        items = [c for c in with_m if c["m"]["buy"] == level]
        items.sort(key=lambda c: (c["m"]["chg_vs_30d"] if c["m"]["chg_vs_30d"] is not None else 0), reverse=reverse)
        if not items:
            return "<li class='muted'>無</li>"
        return "".join(
            f"<li><span>{html.escape(c['name'])}</span><span>{fmt(c['m']['price_7d'])} 元，{fmt_pct(c['m']['chg_vs_30d'])}</span></li>"
            for c in items
        )

    unclassified = [c["name"] for c in with_m if not c["m"]["buy"]]
    buy_ok = buy_list("正常", False)
    if unclassified:
        buy_ok += f"<li class='muted' style='display:block'>資料未滿 30 天，尚未分類：{html.escape('、'.join(unclassified))}</li>"

    alerts = [c for c in with_m if c["m"]["alert"]]
    alerts_html = "".join(
        f"<li class='{alert_class(c['m']['alert'])}'>{html.escape(c['name'])}：{html.escape(c['m']['alert'])}，"
        f"近 7 天 {fmt(c['m']['price_7d'])} 元，近 30 天 {fmt(c['m']['price_30d'])} 元（{fmt_pct(c['m']['chg_vs_30d'])}），"
        f"量 {fmt_pct(c['m']['chg_qty'])}，z {fmt(c['m']['z90'])}</li>"
        for c in alerts
    ) or "<li class='muted'>目前沒有供應警示</li>"

    qty_known = [c for c in with_m if c["m"]["chg_qty"] is not None]
    qty_sorted = sorted(qty_known, key=lambda c: c["m"]["chg_qty"])

    def qty_list(items: list[dict]) -> str:
        if not items:
            return "<li class='muted'>無</li>"
        return "".join(
            f"<li><span>{html.escape(c['name'])}</span><span>{fmt_pct(c['m']['chg_qty'])}，價 {fmt_pct(c['m']['chg_vs_30d'])}</span></li>"
            for c in items
        )

    qty_down = qty_list([c for c in qty_sorted[:6] if c["m"]["chg_qty"] < 0])
    qty_up = qty_list([c for c in reversed(qty_sorted[-6:]) if c["m"]["chg_qty"] > 0])

    news_pool = [c for c in with_m if c["m"]["days_30"] >= 15 and c["m"]["chg_vs_30d"] is not None]
    news_pool.sort(key=lambda c: abs(c["m"]["chg_vs_30d"]), reverse=True)
    news_items = []
    for c in news_pool[:8]:
        m = c["m"]
        cls = "hi" if m["chg_vs_30d"] > 0 else "lo"
        ly_part = f"，較去年同期{word_pct(m['chg_vs_ly'])}" if m["price_ly"] is not None else ""
        news_items.append(
            f"<li class='{cls}'>{html.escape(c['name'])}批發價近一週{word_pct(m['chg_vs_30d'])}：近 7 天全台均價每公斤 {fmt(m['price_7d'])} 元，"
            f"近 30 天 {fmt(m['price_30d'])} 元{ly_part}；交易量較近 30 天{word_pct(m['chg_qty'])}。"
            f"<span class='src'>農業部批發市場行情，資料截至 {html.escape(m['latest_date'])}</span></li>"
        )
    news_html = "".join(news_items) or "<li class='muted'>資料未滿 30 天，新聞線索要等累積。</li>"

    tight = [c for c in with_m if c["m"]["buy"] == "偏貴" and (
        (c["m"]["chg_vs_ly"] is not None and c["m"]["chg_vs_ly"] >= 20) or (c["m"]["z90"] is not None and c["m"]["z90"] >= 1))]
    tight.sort(key=lambda c: c["m"]["chg_vs_30d"] or 0, reverse=True)
    glut = [c for c in with_m if c["m"]["buy"] == "偏便宜"]
    glut.sort(key=lambda c: c["m"]["chg_vs_30d"] or 0)

    def gov_list(items: list[dict]) -> str:
        if not items:
            return "<li class='muted'>無</li>"
        return "".join(
            f"<li><span>{html.escape(c['name'])}</span><span>vs 30 天 {fmt_pct(c['m']['chg_vs_30d'])}，vs 去年 {fmt_pct(c['m']['chg_vs_ly'])}</span></li>"
            for c in items
        )

    def grow_rows(method: str, with_season: bool) -> str:
        out = []
        for c in sorted([c for c in with_m if c["m"]["grow"].get(method)], key=lambda x: x["name"]):
            m = c["m"]
            g = m["grow"][method]
            season_cells = ""
            if with_season:
                now_txt = "適合" if g["in_season"] else ("不適合" if g["in_season"] is False else "-")
                now_cls = "ok" if g["in_season"] else ("lo" if g["in_season"] is False else "")
                season_cells = (f"<td data-v='{dv(g['season'])}'>{html.escape(g['season'] or '-')}</td>"
                                f"<td data-v='{dv(now_txt)}'><span class='tag {now_cls}'>{now_txt}</span></td>")
            out.append(
                f"<tr class='{level_class(m['buy'])}'>{name_cell(c)}"
                f"<td data-v='{dv(m['buy'])}'>{tag(m['buy'])}</td>"
                f"<td data-v='{dv(m['price_7d'])}'>{fmt(m['price_7d'])}</td>"
                f"<td data-v='{dv(m['chg_vs_30d'])}'>{fmt_pct(m['chg_vs_30d'])}</td>"
                f"<td data-v='{dv(g['days'])}'>{fmt(g['days'])} 天</td>"
                f"{season_cells}"
                f"<td data-v='{dv(g['harvest_date'])}'>{html.escape(g['harvest_date'] or '-')}</td>"
                f"<td data-v='{dv(g['price_harvest_ly'])}'>{fmt(g['price_harvest_ly'])}</td>"
                f"<td class='txt' data-v='{dv(g['hint'])}'>{html.escape(g['hint'] or '')}</td></tr>"
            )
        cols = 10 if with_season else 8
        return "".join(out) or f"<tr><td colspan='{cols}' class='muted'>watch_crops.txt 沒有填這種種法的天數</td></tr>"

    cards = []
    for c in ordered:
        m = c["m"]
        if not m:
            continue
        cards.append(
            f"<div class='card'><div class='card-head'><strong>{html.escape(c['name'])}</strong>"
            f"<span class='muted'>最新 {m['latest_date']}，{fmt(m['price_latest'])} 元/公斤</span></div>"
            f"<canvas id='c-{html.escape(c['key'])}' height='140'></canvas></div>"
        )

    return TEMPLATE.substitute(
        date_min=html.escape(s["date_min"]), date_max=html.escape(s["date_max"]), markets=s["markets"],
        generated_at=html.escape(s["generated_at"]), alert_count=s["alert_count"], chart_days=s["chart_days"],
        buy_hi=buy_list("偏貴", True), buy_ok=buy_ok, buy_lo=buy_list("偏便宜", False),
        alerts=alerts_html, qty_down=qty_down, qty_up=qty_up, news=news_html,
        qi_q7=fmt(qi["q7"]), qi_chg30=fmt_pct(qi["chg_30"]), qi_chgly=fmt_pct(qi["chg_ly"]),
        gov_tight=gov_list(tight), gov_glut=gov_list(glut),
        grow_rows_f=grow_rows("field", True), grow_rows_h=grow_rows("hydro", False),
        rows="".join(rows), cards="".join(cards),
        chart_js=CHART_JS, data_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="產生糧食供應監測儀表板")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "foodsec.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "docs"), help="輸出目錄，預設 docs（GitHub Pages 從這裡發佈）")
    ap.add_argument("--watch", default=os.path.join(HERE, "watch_crops.txt"))
    ap.add_argument("--chart-days", type=int, default=180)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"找不到資料庫 {args.db}，先跑 fetch_prices.py", file=sys.stderr)
        return 2
    watch = load_watch(args.watch)
    if not watch:
        print(f"找不到監測作物清單 {args.watch}", file=sys.stderr)
        return 2

    # 唯讀開啟並等鎖，抓取腳本在背景寫入時也能產頁面
    from pathlib import Path
    con = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    (days, crop_count, markets, dmin, dmax) = con.execute(
        "SELECT COUNT(DISTINCT TRANS_DATE), COUNT(DISTINCT CROP_CODE), COUNT(DISTINCT MARKET_CODE), MIN(TRANS_DATE), MAX(TRANS_DATE) FROM PRICE_DAILY"
    ).fetchone()

    crops = []
    series_map: dict[str, list[tuple[str, float, float]]] = {}
    for w in watch:
        series = daily_series(con, w["key"])
        series_map[w["key"]] = series
        m = analyze(series, args.chart_days, w)
        crops.append({"key": w["key"], "name": w["name"], "group": w["group"], "m": m})
    con.close()

    data = {
        "summary": {
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date_min": dmin or "-", "date_max": dmax or "-",
            "trading_days": days or 0, "crop_count": crop_count or 0, "markets": markets or 0,
            "watch_count": len(crops), "alert_count": sum(1 for c in crops if c["m"] and c["m"]["alert"]),
            "chart_days": args.chart_days,
        },
        "qty_index": qty_index(series_map),
        "crops": crops,
    }

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_html(data))

    missing = [c["name"] for c in crops if not c["m"]]
    levels = {lv: sum(1 for c in crops if c["m"] and c["m"]["buy"] == lv) for lv in ("偏貴", "正常", "偏便宜")}
    print(f"已輸出 {os.path.join(args.out, 'index.html')}；監測 {len(crops)} 項，供應警示 {data['summary']['alert_count']} 項，"
          f"偏貴 {levels['偏貴']}、正常 {levels['正常']}、偏便宜 {levels['偏便宜']}"
          + (f"，無資料：{'、'.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
