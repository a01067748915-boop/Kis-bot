"""
변동성 돌파 백테스트 — 미국 주식 (일봉 기준, 서버에서 실행)

  python backtest.py                 # .env의 TARGETS로 최근 3년
  python backtest.py --years 5 --k 0.4
  python backtest.py --sweep         # K값 0.3~0.7 비교

가정 (보수적으로)
  - 목표가 도달 시 목표가 + 슬리피지로 매수, 당일 종가에 매도
  - 같은 날 저가가 손절선 아래면 손절된 것으로 처리 (고가·저가 순서를 모르므로 불리하게)
  - 시가가 이미 목표가×(1+추격한도) 위면 진입 안 함
"""

import argparse
import csv
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def fetch_bars(api, sym, ex, years):
    """KIS 미국 일봉을 100일 단위로 과거로 거슬러 수집"""
    limit = (datetime.now() - timedelta(days=365 * years)).strftime("%Y%m%d")
    bars, base = {}, ""
    while True:
        chunk = api.daily_bars(sym, ex, base)
        new = [b for b in chunk if b["date"] not in bars]
        if not new:
            break
        for b in new:
            bars[b["date"]] = b
        oldest = chunk[0]["date"]
        if oldest <= limit:
            break
        base = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return [bars[d] for d in sorted(bars) if d >= limit]


def load_csv(path):
    with open(path, encoding="utf-8") as f:
        return [{k: (v if k == "date" else float(v)) for k, v in row.items()} for row in csv.DictReader(f)]


def backtest(bars, k=0.5, ma=5, stop_pct=2.0, chase_pct=1.0, fee_pct=0.25, slip_pct=0.05, tax_pct=0.0):
    equity, peak, mdd = 1.0, 1.0, 0.0
    trades, wins, stops = 0, 0, 0
    curve = []
    for i in range(max(2, ma + 1), len(bars)):
        t, y = bars[i], bars[i - 1]
        target = t["open"] + k * (y["high"] - y["low"])
        ret = 0.0
        trend_ok = (not ma) or t["open"] > sum(b["close"] for b in bars[i - ma:i]) / ma
        if trend_ok and t["high"] >= target and t["open"] <= target * (1 + chase_pct / 100):
            entry = max(target, t["open"]) * (1 + slip_pct / 100)
            stop = entry * (1 - stop_pct / 100)
            if t["low"] <= stop:
                exit_ = stop * (1 - slip_pct / 100)
                stops += 1
            else:
                exit_ = t["close"] * (1 - slip_pct / 100)
            ret = exit_ / entry - 1 - fee_pct * 2 / 100 - tax_pct / 100
            trades += 1
            wins += ret > 0
        equity *= 1 + ret
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
        curve.append((t["date"], equity))
    days = len(curve)
    hold = bars[-1]["close"] / bars[max(2, ma + 1)]["open"] - 1 if days else 0
    return {
        "기간": f"{curve[0][0]}~{curve[-1][0]}" if curve else "-",
        "거래일": days, "매매횟수": trades, "승률(%)": round(wins / trades * 100, 1) if trades else 0,
        "손절횟수": stops, "누적수익(%)": round((equity - 1) * 100, 2),
        "최대낙폭(%)": round(mdd * 100, 2), "단순보유(%)": round(hold * 100, 2),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--k", type=float, default=None)
    p.add_argument("--sweep", action="store_true")
    p.add_argument("--csv", help="date,open,high,low,close 형식 CSV로 테스트")
    p.add_argument("--fee", type=float, default=None, help="편도 수수료(%%) 덮어쓰기")
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    k = a.k if a.k is not None else float(g("K", "0.5"))
    opts = dict(ma=int(g("MA_FILTER", "5")), stop_pct=float(g("STOP_LOSS_PCT", "2")),
                chase_pct=float(g("MAX_CHASE_PCT", "1")), fee_pct=float(g("FEE_PCT", "0.25")))

    if a.csv:
        datasets = {Path(a.csv).stem: load_csv(a.csv)}
    else:
        from kis_api import KIS, parse_targets
        api = KIS(g("KIS_ENV", "mock"), g("KIS_APP_KEY"), g("KIS_APP_SECRET"), g("KIS_ACCOUNT"), HERE)
        datasets = {}
        for sym, ex in parse_targets(g("TARGETS", "QQQM:NAS,SOXX:NAS")).items():
            print(f"{sym} 일봉 수집 중…")
            datasets[sym] = fetch_bars(api, sym, ex, a.years)

    if a.fee is not None:
        opts["fee_pct"] = a.fee
    ks = [0.3, 0.4, 0.5, 0.6, 0.7] if a.sweep else [k]
    for code, bars in datasets.items():
        print(f"\n■ {code} ({len(bars)}일)")
        for kk in ks:
            r = backtest(bars, k=kk, **opts)
            print(f"  K={kk}: " + ", ".join(f"{key} {val}" for key, val in r.items()))
    print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다. 슬리피지·체결 지연은 실제로 더 클 수 있습니다.")


if __name__ == "__main__":
    main()
