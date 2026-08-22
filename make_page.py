#!/usr/bin/env python3
"""
把最新读数渲染成一页 —— 输出 docs/index.html，由 GitHub Pages 直接托管。

这一页**刻意不含任何持仓信息**：没有股数、没有现金、没有净值、没有实际权重。
上面每一个数字都是公开市场数据算出来的，所以这一页可以公开挂着，
不需要为了藏它去买 GitHub Pro（Pages 的页面在 Free 和 Pro 下都是公开的，
仓库设 private 只能藏源码，藏不了页面本身）。

代价说清楚：手机上看不到「我该补几股」。那个要么自己拿目标权重乘一下，
要么回本机看。这是用「$0 且不暴露净值」换来的。

自包含：不引外部 CSS/JS/字体，图表是手写的内联 SVG。Pages 是纯静态托管，
少一个外部依赖就少一处会坏的地方。
"""
import csv
import datetime
import glob
import html
import json
import os
import sys
from zoneinfo import ZoneInfo

import brief as B

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DOCS = os.path.join(HERE, "docs")
PT = ZoneInfo("America/Los_Angeles")

CHART_DAYS = 252          # 图上画一年

# 并行对照的记账起点。四条轨道的规则在这一天冻结，此后**不再修改**——
# 改了规则再看成绩，实验就白做了。要换候选就另起一个起点、另开一列。
TRACKS_START = "2026-08-21"


def load():
    with open(os.path.join(DATA, "daily.csv"), newline="") as f:
        rows = [r for r in csv.DictReader(f)]
    briefs = sorted(glob.glob(os.path.join(DATA, "briefs", "*.json")))
    brief = json.load(open(briefs[-1])) if briefs else None
    return rows, brief


def f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def live_quotes():
    """此刻报价 —— 整页唯一会在盘中变动的东西。

    目标权重是收盘价算的，一天只变一次；这里取的是当下价，只为回答一个问题：
    「QQQ 现在离 200 日均线还有多远」。方向条件一旦跌破，目标权重直接归零，
    所以这个距离是唯一值得盘中抬头看一眼的数。

    取不到就返回 None，页面照常出，只是少「此刻」那一块 —— 报价拿不到
    不该让整页挂掉。
    """
    out = {}
    for sym, label in (("QQQ", "qqq"), ("TQQQ", "tqqq")):
        try:
            r = B.yahoo_chart(f"/v8/finance/chart/{sym}"
                              f"?range=1d&interval=5m&includePrePost=true")["chart"]["result"][0]
            meta = r["meta"]
            closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
            px = closes[-1] if closes else meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if px is None:
                return None
            out[label] = {"px": float(px),
                          "chg": (float(px) / float(prev) - 1) * 100 if prev else None,
                          "state": meta.get("marketState", "")}
        except Exception:
            return None
    return out


def session_label():
    """按美东时间自己判交易时段。

    不用 Yahoo 的 marketState —— 这个 chart 端点的 meta 里根本没有那个字段
    （实测返回 None），照抄别处的写法会得到一个空标签。
    只判时段，不判假日：假日里价格不动，页面自己看得出来。
    """
    n = datetime.datetime.now(ZoneInfo("America/New_York"))
    if n.weekday() >= 5:
        return "休市"
    t = n.hour * 60 + n.minute
    if 4 * 60 <= t < 9 * 60 + 30:
        return "盘前"
    if 9 * 60 + 30 <= t < 16 * 60:
        return "盘中"
    if 16 * 60 <= t < 20 * 60:
        return "盘后"
    return "休市"


def sparkline(rows, key, w=680, h=120, band=None):
    """一条折线 + 可选的水平参考带。返回内联 SVG 的 path 数据。"""
    pts = [(i, f(r[key])) for i, r in enumerate(rows) if f(r[key]) is not None]
    if len(pts) < 2:
        return "", 0, 0
    ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    if band:
        lo, hi = min(lo, band[0]), max(hi, band[1])
    span = (hi - lo) or 1.0
    n = len(rows) - 1 or 1
    sx = lambda i: i / n * w
    sy = lambda v: h - (v - lo) / span * h
    d = " ".join(("M" if k == 0 else "L") + f"{sx(i):.1f},{sy(v):.1f}"
                 for k, (i, v) in enumerate(pts))
    return d, lo, hi


def momentum_tracks(rows):
    """四种趋势门的并行纸上记账 —— 全部从主表价格现算，无状态、可重跑。

    背景：2026-08-21 的回测研究发现「趋势还在不在」这个判定有一个反应速度
    的刻度盘：均线快（假警报多、闪崩跑得快），12 个月动量慢（假警报少一半、
    税后多赚约 1.4pp/年，但闪崩多挨 4~10pp）。回测无法替你选速度——快慢
    各赢一种崩盘形状。所以在这里并排记账，让接下来的真实数据投票。

    四条轨道共用同一套仓位规则（min(1, 35% ÷ TQQQ 20日波动率)、3pp 无交易带、
    单边 2bp、现金按年化 4% 计息的近似），唯一差别是趋势判定：
      现行    QQQ > 200 日均线
      对半    均线一票 + 12个月动量一票，各管一半
      三票    3 / 6 / 12 个月动量各一票
      纯动量  QQQ > 12 个月前的价格
    信号一律用前一日收盘；起点日按前一日信号建仓，首日不计成本。
    """
    ds, qs, ts = [], [], []
    for r in rows:
        q, t = f(r.get("qqq")), f(r.get("tqqq"))
        if q is None or t is None:
            continue
        ds.append(r["date"]); qs.append(q); ts.append(t)
    si = next((i for i, d in enumerate(ds) if d >= TRACKS_START), None)
    if si is None or si < 253:
        return None

    def gate(kind, j):
        if j < 252:
            return None
        if kind == "ma":
            return 1.0 if qs[j] > sum(qs[j-199:j+1]) / 200 else 0.0
        if kind == "mom":
            return 1.0 if qs[j] > qs[j-252] else 0.0
        if kind == "blend":
            return (gate("ma", j) + gate("mom", j)) / 2
        if kind == "vote":
            return sum(1 for L in (63, 126, 252) if qs[j] > qs[j-L]) / 3
        if kind == "reenter":
            # 现行＋拐头接：牛市同现行；跌破 200 日线的空仓期里，
            # 只要价格回到 20 日均线上方就先用半仓跟上（回测第五轮的发现：
            # 「抄底」按深度买是接飞刀，按拐头买是把迟到的再入场追回来一半）
            if gate("ma", j) == 1.0:
                return 1.0
            return 0.5 if qs[j] > sum(qs[j-19:j+1]) / 20 else 0.0

    def target(kind, j):
        g = gate(kind, j)
        if g is None or j < 20:
            return None
        rr = [ts[k] / ts[k-1] - 1 for k in range(j-19, j+1)]
        m = sum(rr) / 20
        rv = (sum((x - m) ** 2 for x in rr) / 19) ** .5 * 252 ** .5
        return g * max(0.0, min(1.0, 0.35 / rv)) if rv else None

    out = []
    for kind, name, color in (("ma",    "现行 · 200日均线门", "var(--accent)"),
                              ("blend", "对半 · 均线+动量各一票", "var(--ok)"),
                              ("vote",  "三票 · 3/6/12月动量", "var(--warn)"),
                              ("mom",   "纯动量 · 12个月", "var(--dim)"),
                              ("reenter", "现行＋拐头接 · 空仓期半仓", "#8f6fd8")):
        cur = target(kind, si - 1) or 0.0
        nav = [1.0]
        for i in range(si + 1, len(ds)):
            tgt = target(kind, i - 1)
            if tgt is None:
                tgt = cur
            neww = cur if abs(tgt - cur) < 0.03 else tgt
            r = neww * (ts[i] / ts[i-1] - 1) + (1 - neww) * 0.04 / 252 \
                - abs(neww - cur) * 2e-4
            cur = neww
            nav.append(nav[-1] * (1 + r))
        out.append({"name": name, "color": color,
                    "w": target(kind, len(ds) - 1),
                    "cum": (nav[-1] - 1) * 100, "nav": nav})
    return {"start": ds[si], "days": len(ds) - si, "tracks": out}


def render(live=None):
    rows, brief = load()
    if not rows:
        raise SystemExit("data/daily.csv 是空的，先跑 collect.py")
    last = rows[-1]
    tail = rows[-CHART_DAYS:]

    tw = f(last["target_weight"], 0.0)
    qqq, ma200 = f(last["qqq"]), f(last["ma200"])
    vs200 = f(last["vs_ma200_pct"], 0.0)
    rvol = f(last["tqqq_rvol20"], 0.0)
    direction = str(last["direction_ok"]).lower() == "true"
    asof = last["date"]

    age = (datetime.date.today() - datetime.date.fromisoformat(asof)).days
    stale = age > 4

    # 折线：目标权重（含 0~100% 参考）
    d_w, _, _ = sparkline(tail, "target_weight", band=(0.0, 1.0))
    # 折线：QQQ 距 200 日均线（0 是方向条件的分界）
    d_m, mlo, mhi = sparkline(tail, "vs_ma200_pct", band=(0.0, 0.0))
    zero_y = 120 - (0.0 - mlo) / ((mhi - mlo) or 1) * 120

    m = (brief or {}).get("market", {})
    warn = (brief or {}).get("warnings", []) + (brief or {}).get("blocking", [])

    # 大白话那句：解释为什么权重是现在这个数
    if not direction:
        why = ("QQQ 跌到 200 日均线下面了，规则要求空仓。"
               "这条是方向条件，跌破就归零，不看波动率。")
    elif rvol and rvol > 0.35:
        why = (f"TQQQ 最近 20 天的实际波动是 {rvol*100:.0f}%，高于 35% 这个目标线，"
               f"所以规则自动把仓位压到 {tw*100:.0f}%。波动越大、仓位越小，这是设计如此。")
    else:
        why = (f"TQQQ 波动 {rvol*100:.0f}% 低于 35% 的目标线，规则允许满仓。")

    E = html.escape
    P = []
    A = P.append

    A(f'''<title>REEF 读数</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<style>
:root{{
  --bg:#fbfbfa; --card:#fff; --ink:#1a1a18; --dim:#6b6b66; --line:#e5e4e0;
  --accent:#b8654a; --ok:#3f7a52; --bad:#b2453a; --warn:#8a6d1f;
}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{
  --bg:#191917; --card:#22221f; --ink:#eeede8; --dim:#9b9a93; --line:#33322e;
  --accent:#d98d6e; --ok:#7ab68c; --bad:#e0796c; --warn:#d4b25e;
}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 ui-sans-serif,-apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif;
  -webkit-text-size-adjust:100%}}
.wrap{{max-width:720px;margin:0 auto;padding:20px 16px 56px}}
h1{{font-size:15px;font-weight:600;margin:0;letter-spacing:.02em}}
.sub{{color:var(--dim);font-size:13px;margin-top:2px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:18px;margin-top:14px}}
.hero{{text-align:center;padding:26px 18px}}
.big{{font:600 62px/1 ui-rounded,-apple-system,system-ui,sans-serif;
  letter-spacing:-.02em;color:var(--accent)}}
.big small{{font-size:26px;font-weight:500;margin-left:2px}}
.lbl{{color:var(--dim);font-size:13px;letter-spacing:.06em;text-transform:uppercase}}
.why{{margin-top:14px;font-size:14px;color:var(--ink);text-align:left;
  background:var(--bg);border-radius:10px;padding:12px 14px;line-height:1.65}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td{{padding:9px 0;border-bottom:1px solid var(--line)}}
tr:last-child td{{border-bottom:0}}
td:last-child{{text-align:right;font-variant-numeric:tabular-nums;font-weight:500}}
.k{{color:var(--dim)}}
.ok{{color:var(--ok)}} .bad{{color:var(--bad)}} .warn{{color:var(--warn)}}
.chart{{margin-top:6px;overflow-x:auto}}
svg{{display:block;width:100%;height:auto}}
.cap{{color:var(--dim);font-size:12px;margin-top:6px}}
.note{{color:var(--dim);font-size:12px;line-height:1.7;margin-top:22px}}
.pill{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
  border:1px solid var(--line);color:var(--dim)}}
.flag{{background:#b2453a12;border:1px solid #b2453a44;color:var(--bad);
  border-radius:10px;padding:11px 13px;margin-top:12px;font-size:13px}}
</style>

<div class="wrap">
  <h1>REEF 读数</h1>
  <div class="sub">数据日 {E(asof)} · {"⚠️ 已 %d 天没更新" % age if stale else "第 %d 天" % age if age else "今日"}</div>
''')

    if stale:
        A(f'<div class="flag">主表最新一行是 {E(asof)}，距今 {age} 天。'
          f'采集可能已经停摆——去 Actions 页看一眼是不是报红了。</div>')
    for w in warn[:3]:
        A(f'<div class="flag">{E(str(w))}</div>')

    A(f'''
  <div class="card hero">
    <div class="lbl">今日目标权重 · TQQQ</div>
    <div class="big">{tw*100:.1f}<small>%</small></div>
    <div class="sub">规则：min(100%, 35% ÷ TQQQ 20日波动率)，QQQ 跌破 200 日均线则归零</div>
    <div class="why">{E(why)}</div>
  </div>

''')

    if live and ma200:
        lq, lt = live["qqq"], live["tqqq"]
        to_ma = (lq["px"] / ma200 - 1) * 100
        sess = session_label()
        A(f'''  <div class="card">
    <div class="lbl">此刻 · {E(sess)}</div>
    <table>
      <tr><td class="k">QQQ</td><td>{lq["px"]:,.2f}
        <span class="{'ok' if (lq["chg"] or 0)>=0 else 'bad'}">{lq["chg"]:+.2f}%</span></td></tr>
      <tr><td class="k">TQQQ</td><td>{lt["px"]:,.2f}
        <span class="{'ok' if (lt["chg"] or 0)>=0 else 'bad'}">{lt["chg"]:+.2f}%</span></td></tr>
      <tr><td class="k">距 200 日均线</td><td class="{'ok' if to_ma>0 else 'bad'}">{to_ma:+.2f}%</td></tr>
    </table>
    <div class="cap">整页只有这一块是盘中会动的。目标权重永远是收盘价算的，一天只变一次——
      盘中价再怎么跳都不会改它。这里看的是方向条件还剩多少余量：跌到 0% 以下，明天的目标权重就归零。</div>
  </div>
''')

    A(f'''  <div class="card">
    <table>
      <tr><td class="k">方向条件</td><td class="{'ok' if direction else 'bad'}">
        {'满足（QQQ 在 200 日均线上方）' if direction else '不满足（QQQ 已跌破 200 日均线）'}</td></tr>
      <tr><td class="k">QQQ</td><td>{qqq:,.2f}</td></tr>
      <tr><td class="k">200 日均线</td><td>{ma200:,.2f}</td></tr>
      <tr><td class="k">距均线</td><td class="{'ok' if vs200>0 else 'bad'}">{vs200:+.2f}%</td></tr>
      <tr><td class="k">TQQQ 20 日波动率</td><td class="{'warn' if rvol>.35 else ''}">{rvol*100:.1f}%</td></tr>
      <tr><td class="k">VIX</td><td>{f(last['vix'],0):.2f} · 两年分位 {f(last['vix_pctile_2y'],0):.0f}%</td></tr>
''')
    A('    </table>\n  </div>\n')

    if d_w:
        A(f'''  <div class="card">
    <div class="lbl">目标权重 · 近一年</div>
    <div class="chart"><svg viewBox="0 0 680 120" preserveAspectRatio="none" aria-label="目标权重历史">
      <path d="{d_w}" fill="none" stroke="var(--accent)" stroke-width="2"
            stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    </svg></div>
    <div class="cap">上沿 100%、下沿 0%。掉到底部的那些段是方向条件不满足、规则要求空仓的日子。</div>
  </div>
''')
    if d_m:
        A(f'''  <div class="card">
    <div class="lbl">QQQ 距 200 日均线 · 近一年</div>
    <div class="chart"><svg viewBox="0 0 680 120" preserveAspectRatio="none" aria-label="QQQ 距均线历史">
      <line x1="0" y1="{zero_y:.1f}" x2="680" y2="{zero_y:.1f}"
            stroke="var(--dim)" stroke-width="1" stroke-dasharray="3 3"
            vector-effect="non-scaling-stroke"/>
      <path d="{d_m}" fill="none" stroke="var(--ok)" stroke-width="2"
            stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    </svg></div>
    <div class="cap">虚线是 200 日均线本身。线在虚线下方 = 方向条件不满足 = 目标权重归零。</div>
  </div>
''')

    trk = momentum_tracks(rows)
    if trk:
        A(f'''  <div class="card">
    <div class="lbl">并行对照（纸上实验） · 第 {trk["days"]} 个交易日</div>
    <table>
''')
        for t in trk["tracks"]:
            wtxt = f"{t['w']*100:.1f}%" if t["w"] is not None else "—"
            cum = t["cum"]
            cls = "ok" if cum > 0 else ("bad" if cum < 0 else "")
            A(f'''      <tr><td class="k"><span style="color:{t["color"]}">●</span> {E(t["name"])}</td>
        <td>{wtxt} <span class="{cls}" style="margin-left:8px">{cum:+.2f}%</span></td></tr>
''')
        A('    </table>\n')
        if trk["days"] >= 6:
            allv = [v for t in trk["tracks"] for v in t["nav"]]
            lo, hi = min(allv), max(allv)
            span = (hi - lo) or 1.0
            n = max(len(trk["tracks"][0]["nav"]) - 1, 1)
            paths = ""
            for t in trk["tracks"]:
                d = " ".join(("M" if k == 0 else "L")
                             + f"{k/n*680:.1f},{120-(v-lo)/span*120:.1f}"
                             for k, v in enumerate(t["nav"]))
                paths += (f'<path d="{d}" fill="none" stroke="{t["color"]}" stroke-width="2" '
                          f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>')
            A(f'''    <div class="chart"><svg viewBox="0 0 680 120" preserveAspectRatio="none"
      aria-label="并行对照净值">{paths}</svg></div>
''')
        A(f'''    <div class="cap">同一套仓位规则，五条轨道并排记账（每格 = 最新目标权重 · 自
      {E(trk["start"])} 累计）：前四条比「趋势还在不在」由谁判定（均线快、动量慢，快慢各赢一种
      崩盘），第五条比「空仓之后多快回来」（跌破期间价格回到 20 日线上方就先半仓跟上）。
      规则已冻结，此后不改——这里让往后的真实行情投票。没跑过至少一次像样的回调之前，
      别急着读结论；实际执行仍按上面的现行规则。</div>
  </div>
''')

    live_n = sum(1 for r in rows if r["source"] == "live")
    A(f'''
  <div class="card">
    <table>
      <tr><td class="k">主表行数</td><td>{len(rows):,}（{E(rows[0]["date"])} 起 · 实采 {live_n}）</td></tr>
      <tr><td class="k">页面生成</td><td>{datetime.datetime.now(PT):%m-%d %H:%M} PT</td></tr>
    </table>
  </div>

  <div class="note">
    <span class="pill">不含持仓</span>
    这一页上没有股数、现金、净值和实际权重——全是公开市场数据算出来的，所以能公开挂着。
    「我该补几股」要自己拿目标权重乘一下，或者回本机看。<br><br>
    数据每个交易日收盘后由 GitHub Actions 自动采集，本机关机不影响。
    以上是公开数据的机械计算结果，不构成投资建议。杠杆 ETF 风险极高。
  </div>
</div>''')

    os.makedirs(DOCS, exist_ok=True)
    out = os.path.join(DOCS, "index.html")
    body = "".join(P)
    page = "<!doctype html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\">" + body + "</html>\n"
    # 和 collect.py 一样：内容没变就不落盘，避免只改时间戳的空提交。
    # 页面里唯一会自己动的是「页面生成」时间，所以比对时把它剔掉。
    key = lambda s: "\n".join(l for l in s.splitlines() if "页面生成" not in l)
    if os.path.exists(out):
        with open(out) as fh:
            if key(fh.read()) == key(page):
                print(f"页面无变化，未改动 {out}")
                return
    with open(out, "w") as fh:
        fh.write(page)
    print(f"已写出 {out}　目标权重 {tw*100:.1f}%　数据日 {asof}")


if __name__ == "__main__":
    # --live 多花约 2 秒取一次当下报价。给每小时那条 workflow 用；
    # 收盘后的采集流水线不需要，它出的是收盘口径。
    render(live_quotes() if "--live" in sys.argv else None)
