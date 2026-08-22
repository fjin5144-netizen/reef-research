#!/usr/bin/env python3
"""
REEF 研究数据采集 —— 每个交易日收盘后跑一次就够。

为什么不是每 15 分钟轮询一次：
  盘中读数**不是快照型数据**。Yahoo 的 15 分钟 K 线可以回头拉 60 天，
  日线更是想拉多久拉多久。收盘后一次性取，和盘中戳 26 次拿到的东西
  一模一样，但没有 GitHub cron 丢档的问题，也不用每天几十个提交。

  所有列都是价格的纯函数，事后都能重算 —— 没有任何一样东西是
  「过期不候、只能当场抓」的，所以漏采一天也丢不了数据。

自愈：每次正常运行都会顺手重写最近 5 个交易日的盘中文件。也就是说连续
四天 cron 全丢，第五天成功那次会把前面全补回来，不需要人工干预。

产出（全部可重复：同一天跑两次结果一致，不会追加重复行）
  data/daily.csv              研究主表，一天一行
  data/briefs/<日期>.json     当日完整读数（daily.csv 的超集）
  data/intraday/<日期>.csv    当日 15 分钟 K 线，QQQ/TQQQ/VIX，含盘前盘后

用法
  python3 collect.py              采集最近一个交易日
  python3 collect.py --backfill   拉满 60 天盘中历史，并用日线重建历史主表
  python3 collect.py --check      只做新鲜度检查（给 workflow 报警用），不写文件
"""
import argparse
import csv
import datetime
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import brief as B

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SPINE = os.path.join(DATA, "daily.csv")
ET = ZoneInfo("America/New_York")

INTRADAY_SYMBOLS = [("QQQ", "QQQ"), ("TQQQ", "TQQQ"), ("%5EVIX", "VIX")]

# 重建历史主表时往回取多少日历天。前 219 个交易日要拿来喂 200 日均线和它的
# 上行判定，出不了行，所以实际可用历史比这个数短约一年。
RECON_DAYS = 4000
VIX_PCTILE_BARS = 504     # 两年 ≈ 504 个交易日

# 顺序固定 —— 改动这个列表等于改动 daily.csv 的表头，会让历史行对不上。
# 要加列就往**末尾**加，不要插在中间。
SPINE_COLS = [
    "date", "source",
    "qqq", "tqqq", "vix",
    "ma50", "ma100", "ma200", "ma200_rising",
    "vs_ma50_pct", "vs_ma100_pct", "vs_ma200_pct",
    "vix_pctile_2y", "tqqq_rvol20", "tqqq_rvol60",
    "qqq_dd_1y", "tqqq_dd_1y",
    "direction_ok", "target_weight",
    "n_blocking", "n_warnings", "collected_at",
]


def log(m):
    print(f"[{datetime.datetime.now(ET):%Y-%m-%d %H:%M:%S ET}] {m}", flush=True)


def r6(x):
    return round(x, 6) if isinstance(x, float) else x


def write_json_stable(path, obj, volatile):
    """写 JSON，但若除 volatile 字段外内容与磁盘上完全一致，就一个字节都不动。

    收盘后排了 4 档 cron，第一档落地之后，后面三档除了时间戳没有任何新东西。
    不做这个，每天就会多出 3 个「只改了一行时间」的提交，一年 750 个纯噪音。
    """
    if os.path.exists(path):
        try:
            with open(path) as f:
                old = json.load(f)
            strip = lambda d: {k: v for k, v in d.items() if k not in volatile}
            if strip(old) == strip(obj):
                return False
        except Exception:
            pass          # 读不动就当它不存在，重写一份
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    return True


# ---------------------------------------------------------------- 盘中 K 线
def fetch_intraday(days):
    """取 15 分钟 K 线，按交易日分组。含盘前盘后 —— 事后可以过滤掉，
    但当时没存下来就再也补不回来了（超过 60 天 Yahoo 就不给了）。"""
    by_day = {}
    for sym, label in INTRADAY_SYMBOLS:
        d = B.yahoo_chart(f"/v8/finance/chart/{sym}"
                          f"?range={days}d&interval=15m&includePrePost=true")
        res = d["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        n = 0
        for i, t in enumerate(ts):
            c = q["close"][i]
            if c is None:
                continue
            dt = datetime.datetime.fromtimestamp(t, ET)
            by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append({
                "ts_et": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": t,
                "symbol": label,
                "open": r6(q["open"][i]), "high": r6(q["high"][i]),
                "low": r6(q["low"][i]), "close": r6(c),
                "volume": q["volume"][i],
            })
            n += 1
        log(f"  {label}: {n} 根 15 分钟 K 线")
        time.sleep(1)   # 别把 Yahoo 惹毛，机房 IP 本来就更容易被限流
    return by_day


def write_intraday(by_day):
    d = os.path.join(DATA, "intraday")
    os.makedirs(d, exist_ok=True)
    for day, rows in sorted(by_day.items()):
        rows.sort(key=lambda r: (r["epoch"], r["symbol"]))
        p = os.path.join(d, f"{day}.csv")
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts_et", "symbol", "open", "high",
                                              "low", "close", "volume"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return len(by_day)


# ---------------------------------------------------------------- 主表
def read_spine():
    if not os.path.exists(SPINE):
        return {}
    with open(SPINE, newline="") as f:
        return {r["date"]: r for r in csv.DictReader(f)}


def write_spine(rows):
    os.makedirs(DATA, exist_ok=True)
    with open(SPINE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SPINE_COLS, extrasaction="ignore")
        w.writeheader()
        for d in sorted(rows):
            w.writerow({c: rows[d].get(c, "") for c in SPINE_COLS})


def spine_row_from_brief(b, collected_at):
    """live 行：直接来自当日 build()，所有列都有值。"""
    m, rf = b["market"], b.get("reef", {})
    return {
        "date": m["asof"], "source": "live",
        "qqq": r6(m["qqq"]), "tqqq": r6(m["tqqq"]), "vix": r6(m["vix"]),
        "ma50": r6(m["ma50"]), "ma100": r6(m["ma100"]), "ma200": r6(m["ma200"]),
        "ma200_rising": m["ma200_rising"],
        "vs_ma50_pct": r6(m["vs_ma50_pct"]), "vs_ma100_pct": r6(m["vs_ma100_pct"]),
        "vs_ma200_pct": r6(m["vs_ma200_pct"]),
        # 用 _pit 而不是 m["vix_pctile_2y"]：主表必须和事后重建的历史行同口径，
        # 否则同一列里 live 行和 reconstructed 行是两个定义。原口径仍在 brief JSON 里。
        "vix_pctile_2y": r6(m["vix_pctile_2y_pit"]),
        "tqqq_rvol20": r6(m["tqqq_rvol20"]), "tqqq_rvol60": r6(m["tqqq_rvol60"]),
        "qqq_dd_1y": r6(m["qqq_dd_from_1y_high"]), "tqqq_dd_1y": r6(m["tqqq_dd_from_1y_high"]),
        "direction_ok": rf.get("direction_ok", ""), "target_weight": rf.get("target_weight", ""),
        "n_blocking": len(b.get("blocking", [])), "n_warnings": len(b.get("warnings", [])),
        "collected_at": collected_at,
    }


def reconstruct_spine(collected_at):
    """用日线重建历史主表。

    价格派生的那些列（均线、波动率、回撤、分位）全是价格的纯函数，事后
    算得出来，所以第一天就能拿到两年多的历史，不用从零攒。

    source 列标成 reconstructed，别和 live 行混着用。

    与 build() 的差别只有一处，而且是**更严格**的方向：这里的 vix_pctile_2y
    是 point-in-time、严格取当日往前 504 个交易日（两年）算的；build() 用的
    是「当下拿到的全部 800 日历天」，实际约 549 个交易日。做研究要的是前者，
    不然既有前视偏差、名字也对不上。两者数值会有小差异，属预期之内。
    """
    qqq = dict(B.yahoo("QQQ", RECON_DAYS))
    tqqq = dict(B.yahoo("TQQQ", RECON_DAYS))
    vix = dict(B.yahoo("%5EVIX", RECON_DAYS))
    dates = sorted(set(qqq) & set(tqqq) & set(vix))
    log(f"  日线：QQQ {len(qqq)} / TQQQ {len(tqqq)} / VIX {len(vix)}，三者齐全 {len(dates)} 天")

    qs = [qqq[d] for d in dates]
    ts = [tqqq[d] for d in dates]
    vs = [vix[d] for d in dates]

    rows = {}
    for i, d in enumerate(dates):
        if i < 219:                       # 不够算 200 日均线 + 上行判定
            continue
        q, t, v = qs[:i + 1], ts[:i + 1], vs[:i + 1]
        ma200, ma100, ma50 = B.sma(q, 200), B.sma(q, 100), B.sma(q, 50)
        ma200_prev = sum(q[-220:-20]) / 200 if len(q) >= 220 else None
        rv20, rv60 = B.realized_vol(t, 20), B.realized_vol(t, 60)
        if ma200 is None or rv20 is None:
            continue
        direction_ok = q[-1] > ma200
        rows[d] = {
            "date": d, "source": "reconstructed",
            "qqq": r6(q[-1]), "tqqq": r6(t[-1]), "vix": r6(v[-1]),
            "ma50": r6(ma50), "ma100": r6(ma100), "ma200": r6(ma200),
            "ma200_rising": (ma200 > ma200_prev) if ma200_prev else "",
            "vs_ma50_pct": r6((q[-1] / ma50 - 1) * 100),
            "vs_ma100_pct": r6((q[-1] / ma100 - 1) * 100),
            "vs_ma200_pct": r6((q[-1] / ma200 - 1) * 100),
            "vix_pctile_2y": r6(B.pctile(v[-VIX_PCTILE_BARS:], v[-1])),
            "tqqq_rvol20": r6(rv20), "tqqq_rvol60": r6(rv60) if rv60 else "",
            "qqq_dd_1y": r6((q[-1] / max(q[-252:]) - 1) * 100),
            "tqqq_dd_1y": r6((t[-1] / max(t[-252:]) - 1) * 100),
            "direction_ok": direction_ok,
            "target_weight": round(0.0 if not direction_ok else min(1.0, 0.35 / rv20), 4),
            "n_blocking": "", "n_warnings": "", "collected_at": collected_at,
        }
    return rows


# ---------------------------------------------------------------- 新鲜度
def check(max_age_days=4):
    spine = read_spine()
    live = sorted(d for d, r in spine.items() if r.get("source") == "live")
    if not live:
        print("主表里还没有 live 行 —— 如果刚建好仓库，这是正常的。")
        return 0
    last = datetime.date.fromisoformat(live[-1])
    age = (datetime.date.today() - last).days
    print(f"最新 live 数据 {last}，距今 {age} 天（共 {len(spine)} 行，其中 live {len(live)} 行）")
    if age > max_age_days:
        print(f"::error::采集已停摆：最新数据 {last}，距今 {age} 天（阈值 {max_age_days}）。"
              f"多半是 Yahoo 把机房 IP 限流了。")
        return 1
    return 0


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="拉满 60 天盘中历史，并用日线重建历史主表")
    ap.add_argument("--check", action="store_true", help="只做新鲜度检查，不写文件")
    a = ap.parse_args()

    if a.check:
        sys.exit(check())

    collected_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1) 当日读数 —— 这一步失败就整个失败，宁可 workflow 报红让你收到邮件，
    #    也不要悄悄提交一份缺数据的表。
    log("取当日读数…")
    b = B.build()
    asof = b["market"]["asof"]
    log(f"  数据日 {asof}　QQQ {b['market']['qqq']:.2f}　"
        f"目标权重 {b.get('reef', {}).get('target_weight_pct', '—')}%")
    for x in b.get("blocking", []):
        log(f"  🛑 {x}")
    for x in b.get("warnings", []):
        log(f"  ⚠️  {x}")

    os.makedirs(os.path.join(DATA, "briefs"), exist_ok=True)
    write_json_stable(os.path.join(DATA, "briefs", f"{asof}.json"), b, {"generated"})

    # 2) 盘中 K 线。平时拉 5 天（自愈：补上前几次丢掉的 cron），
    #    --backfill 拉满 Yahoo 给的 60 天。
    days = 60 if a.backfill else 5
    log(f"取 15 分钟 K 线（最近 {days} 天）…")
    n = write_intraday(fetch_intraday(days))
    log(f"  写出 {n} 个交易日的盘中文件")

    # 3) 主表
    spine = read_spine()
    if a.backfill:
        log("用日线重建历史主表…")
        rebuilt = reconstruct_spine(collected_at)
        # 已有的 live 行仍然优先：它是当天实时取的，重建版是事后算的。
        for d, r in rebuilt.items():
            if spine.get(d, {}).get("source") != "live":
                spine[d] = r
        log(f"  重建 {len(rebuilt)} 行")
    row = spine_row_from_brief(b, collected_at)
    prev = spine.get(asof)
    # 读数一个字没变就沿用旧时间戳 —— 同上，避免只改时间戳的空提交。
    if prev and all(str(prev.get(c, "")) == str(row.get(c, ""))
                    for c in SPINE_COLS if c != "collected_at"):
        row["collected_at"] = prev["collected_at"]
    spine[asof] = row
    write_spine(spine)
    live_n = sum(1 for r in spine.values() if r.get("source") == "live")
    log(f"主表 {len(spine)} 行（live {live_n}），最新 {asof}")


if __name__ == "__main__":
    main()
