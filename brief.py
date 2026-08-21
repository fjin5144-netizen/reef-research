#!/usr/bin/env python3
"""
每日读数的计算部分 —— 从 Investing/strategy/daily_brief.py 原样搬来。

只保留 build()，去掉了终端渲染和 --save：那两块是给人看的，research 不需要。
**算法一个字没改**，所以这里产出的 brief JSON 与本机 briefs/ 里的那份可以直接对比；
如果哪天两边对不上，那是数据源的问题，不是两套代码跑偏了。

改动仅限取数层：
  · fetch 重试次数加倍，并在 query1 失败后自动换 query2
    （GitHub 的机房 IP 被 Yahoo 限流的概率远高于家里宽带）
  · utcfromtimestamp → 带时区的写法，避免 3.12 的 DeprecationWarning 刷屏
    输出的日期字符串完全一致

纯标准库，workflow 里不需要 pip install 任何东西。
"""
import csv
import datetime
import json
import math
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


# ---------------------------------------------------------------- 数据获取
def _get(url, timeout=45):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def fetch_json(url, tries=4):
    for a in range(tries):
        try:
            return json.loads(_get(url))
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(3 * (a + 1))


def yahoo_chart(path):
    """在两个 Yahoo 主机之间轮换重试。path 形如 '/v8/finance/chart/QQQ?...'"""
    last = None
    for attempt in range(6):
        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        try:
            return json.loads(_get(f"https://{host}{path}"))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Yahoo 取数失败（6 次，两个主机都试过）：{last}")


def yahoo(sym, days=800):
    p2 = int(time.time())
    p1 = p2 - days * 86400
    d = yahoo_chart(f"/v8/finance/chart/{sym}"
                    f"?period1={p1}&period2={p2}&interval=1d")["chart"]["result"][0]
    ts, q = d["timestamp"], d["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        out.append((datetime.datetime.fromtimestamp(
            t, datetime.timezone.utc).strftime("%Y-%m-%d"), float(q["close"][i])))
    return out


def raincheck_current():
    return fetch_json("https://raincheck.fund/api/current")


def raincheck_performance():
    raw = _get("https://raincheck.fund/api/performance").decode()
    return list(csv.DictReader(raw.splitlines()))


# ---------------------------------------------------------------- 指标
def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def realized_vol(v, n=20):
    if len(v) < n + 1:
        return None
    r = [v[i] / v[i - 1] - 1 for i in range(len(v) - n, len(v))]
    m = sum(r) / len(r)
    return (sum((x - m) ** 2 for x in r) / (len(r) - 1)) ** .5 * math.sqrt(252)


def pctile(v, x):
    return 100.0 * sum(1 for y in v if y <= x) / len(v)


def parse_trend_table(html):
    """从 /api/current 的 trend_table HTML 里抽出当前趋势逐日行"""
    import re, html as H
    rows = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        c = [H.unescape(re.sub("<[^>]+>", "", x)).strip()
             for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        c = [x for x in c if x]
        # Google Sheets 导出会带一列行号，先定位趋势计数列再对齐
        k = next((j for j, x in enumerate(c) if "📈" in x or "📉" in x), None)
        if k is not None and len(c) - k >= 6:
            rows.append(c[k - 1:k + 6])   # 信号, 趋势计数, QQQ收盘, QQQ累计, 杠杆收盘, 杠杆累计, 日期
    return rows


# ---------------------------------------------------------------- 主流程
def build():
    out = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "warnings": [], "blocking": []}
    W, BLK = out["warnings"], out["blocking"]

    try:
        cur = raincheck_current()
    except Exception as e:
        cur = {}
        W.append(f"Raincheck API 不可用（{type(e).__name__}）——本次跳过官方信号与交叉核对，"
                 f"REEF 指标不受影响。若持续失败，可能是他们给端点加了认证。")
    trend = parse_trend_table(cur.get("trend_table", "")) if cur else []
    out["raincheck"] = {"is_paid": cur.get("is_paid"), "chart": cur.get("chart_image_url")}
    if trend:
        last = trend[-1]
        up = "📈" in last[1]
        out["raincheck"].update({
            "signal": last[0], "trend_count": last[1], "direction": "UPTREND" if up else "DOWNTREND",
            "day1": trend[0][6], "asof": last[6],
            "qqq_close": last[2], "qqq_cum": last[3],
            "lev_close": last[4], "lev_cum": last[5],
            "n_days": len(trend),
            "signal_path": [r[0] for r in trend][-10:],
        })

    qqq = yahoo("QQQ", 800)
    tqqq = yahoo("TQQQ", 800)
    vix = yahoo("%5EVIX", 800)
    qc = [c for _, c in qqq]
    tc = [c for _, c in tqqq]
    vc = [c for _, c in vix]

    ma200, ma100, ma50 = sma(qc, 200), sma(qc, 100), sma(qc, 50)
    ma200_prev = sum(qc[-220:-20]) / 200 if len(qc) >= 220 else None
    rv20 = realized_vol(tc, 20)
    rv60 = realized_vol(tc, 60)

    out["market"] = {
        "asof": qqq[-1][0],
        "qqq": qc[-1],
        "tqqq": tc[-1],
        "vix": vc[-1],
        "vix_pctile_2y": pctile(vc, vc[-1]),
        "ma50": ma50, "ma100": ma100, "ma200": ma200,
        "vs_ma200_pct": (qc[-1] / ma200 - 1) * 100 if ma200 else None,
        "vs_ma100_pct": (qc[-1] / ma100 - 1) * 100 if ma100 else None,
        "vs_ma50_pct": (qc[-1] / ma50 - 1) * 100 if ma50 else None,
        "ma200_rising": (ma200 > ma200_prev) if (ma200 and ma200_prev) else None,
        "away_alert": {lab: round(ma200 * (1 + p), 0)
                       for lab, p in (("1周", .009), ("2周", .018),
                                      ("4周", .037), ("8周", .075))} if ma200 else None,
        "tqqq_rvol20": rv20, "tqqq_rvol60": rv60,
        "qqq_dd_from_1y_high": (qc[-1] / max(qc[-252:]) - 1) * 100,
        "tqqq_dd_from_1y_high": (tc[-1] / max(tc[-252:]) - 1) * 100,
    }

    # 独立规则 vs 黑箱信号 的一致性
    my_regime = "RISK-ON" if (ma200 and qc[-1] > ma200) else "RISK-OFF"
    their = out["raincheck"].get("direction")
    out["cross_check"] = {
        "my_ma200_regime": my_regime,
        "raincheck_regime": their,
        "agree": ((my_regime == "RISK-ON") == (their == "UPTREND")) if their else None,
        "unavailable": their is None,
    }

    # ---- 数据健康检查（防止按错误数据算读数）----
    today = datetime.date.today()
    asof = datetime.date.fromisoformat(qqq[-1][0])
    stale = (today - asof).days
    if stale > 4:
        BLK.append(f"行情数据已过期 {stale} 天（最新 {asof}）。可能是数据源故障，"
                   f"不要按本次输出调仓。")
    elif stale > 1:
        W.append(f"行情数据为 {asof}，距今 {stale} 天（可能是周末或休市）。")

    def wild(series, name, lim):
        out_ = []
        for i in range(max(1, len(series) - 21), len(series)):
            mv = series[i] / series[i - 1] - 1
            if abs(mv) > lim:
                out_.append((i, round(mv * 100, 1)))
        return out_
    tq_wild = wild(tc, "TQQQ", 0.45)
    qq_wild = wild(qc, "QQQ", 0.18)
    if tq_wild or qq_wild:
        BLK.append(f"近 21 日内出现异常价格跳变（TQQQ {tq_wild} / QQQ {qq_wild}）。"
                   f"3× ETF 单日超过 ±45% 极可能是**拆股未调整**或数据错误——"
                   f"它会让波动率虚高、仓位被错误地降到接近 0。请先人工核实价格，不要据此交易。")

    if len(qc) < 220:
        BLK.append(f"QQQ 历史只有 {len(qc)} 根 K 线，不足以计算 200 日均线。")

    # ---- REEF 今日目标权重 ----
    if not BLK and ma200 is not None and rv20 is not None:
        direction_ok = qc[-1] > ma200
        w = 0.0 if not direction_ok else min(1.0, 0.35 / rv20)
        out["reef"] = {
            "direction_ok": direction_ok,
            "target_weight": round(w, 4),
            "target_weight_pct": round(w * 100, 1),
            "rule": "min(1, 35% ÷ TQQQ 20日年化波动率)，方向不满足则为 0",
            "note": "这是规则算出的目标权重，不是建议仓位。是否执行、用多少本金，由你自己决定。",
        }
    else:
        out["reef"] = {"error": "数据检查未通过，本次不输出目标权重"}

    # 波动率目标建议敞口（仅作参考刻度，不是建议仓位）
    if rv20:
        out["vol_target"] = {
            "target_vol": 0.35,
            "implied_weight": round(min(1.0, 0.35 / rv20), 3),
            "note": "35% 年化波动为目标时，TQQQ 的等风险权重；回测中这是唯一在 1999-2026 全周期稳健降低回撤的改动",
        }

    # 距离分档止盈线
    if trend:
        try:
            cum = float(out["raincheck"]["lev_cum"].replace("%", ""))
            out["profit_ladder"] = {f"+{k}%": round(k - cum, 2) for k in (10, 20, 30)}
            out["raincheck"]["lev_cum_num"] = cum
        except Exception:
            pass

    # 趋势长度的历史分位
    try:
        perf = raincheck_performance()
        lens, cur_len, cur_dir = [], 0, None
        for r in perf:
            tcnt = r["Trend Count"]
            d = "up" if "📈" in tcnt else "down"
            n = "".join(ch for ch in tcnt if ch.isdigit())
            if n == "1":
                if cur_dir:
                    lens.append((cur_dir, cur_len))
                cur_dir, cur_len = d, 1
            else:
                cur_len += 1
        if cur_dir:
            lens.append((cur_dir, cur_len))
        ups = sorted(l for d, l in lens if d == "up")
        out["trend_length_history"] = {
            "past_uptrends": ups,
            "median": ups[len(ups) // 2] if ups else None,
            "current_day": out["raincheck"].get("n_days"),
            "pctile": round(pctile(ups, out["raincheck"].get("n_days", 0)), 1) if ups else None,
        }
        out["perf_api_asof"] = perf[-1]["Date"]
    except Exception as e:
        out["trend_length_history"] = {"error": str(e)}

    return out


if __name__ == "__main__":
    print(json.dumps(build(), indent=1, ensure_ascii=False))
