"""
변동성 돌파 백테스트 — 미국 주식 (일봉 기준, 서버에서 실행)

  python backtest.py                 # .env의 TARGETS로 최근 3년
  python backtest.py --years 5 --k 0.4
  python backtest.py --sweep         # K값 0.3~1.0 비교
  python backtest.py --compare       # 청산 방식·추세필터별 최적 K와 수익 범위를 한눈에 비교
  python backtest.py --compare --targets QQQM:NAS,SOXX:NAS --stop 8
  python backtest.py --exit next_open --ma 20 --sweep

가정
  - 목표가 도달 시 목표가 + 슬리피지로 매수
  - 청산: close = 당일 종가(현재 봇 방식), next_open = 다음 날 시가(밤사이 상승분 포함, 원래 변동성 돌파 방식)
  - 시가가 이미 목표가×(1+추격한도) 위면 진입 안 함
  - 일봉으로는 저가·고가 중 무엇이 먼저인지 몰라 손절 판단을 두 가지로 계산해 범위로 보여줌
      최악: 그날 저가가 손절선 아래면 손절 (저가가 매수 후에 나왔다고 가정)
      최선: 종가가 손절선 아래일 때만 손절 (저가가 매수 전에 나왔다고 가정)
    실제 성과는 대개 이 둘 사이
"""

import argparse
import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def fetch_bars(api, sym, ex, years, adjusted=True, cache=HERE / "data" / "kis"):
    """KIS 미국 일봉 최근 years년 (100일 단위로 이어 붙이며 분할 기준 차이 보정)
    adjusted=False 면 그날 실제 가격(분할 미반영). 받은 결과는 그날 하루 동안 저장해 다시 실행할 때 재사용"""
    limit = (datetime.now() - timedelta(days=365 * years)).strftime("%Y%m%d")
    path = Path(cache) / f"{sym}_{ex}_{'adj' if adjusted else 'raw'}_{years}y.json" if cache else None
    today = datetime.now().strftime("%Y%m%d")
    if path and path.exists():
        try:
            saved = json.loads(path.read_text())
            if saved.get("day") == today:
                return saved["bars"]
        except (ValueError, KeyError):
            pass
    bars = api.daily_history(sym, ex, since=limit) if adjusted else api.daily_history(sym, ex, since=limit,
                                                                                      adjusted=False)
    if path and bars:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"day": today, "bars": bars}))
    return bars


def load_csv(path):
    with open(path, encoding="utf-8") as f:
        return [{k: (v if k == "date" else float(v)) for k, v in row.items()} for row in csv.DictReader(f)]


def backtest(bars, k=0.5, ma=5, stop_pct=2.0, chase_pct=1.0, fee_pct=0.25, slip_pct=0.05, tax_pct=0.0,
             exit="close", optimistic=False):
    """exit: "close"(당일 종가) / "next_open"(다음 날 시가). optimistic: 손절을 유리하게 판단"""
    if exit not in ("close", "next_open"):
        raise ValueError("exit 는 close 또는 next_open")
    equity, peak, mdd = 1.0, 1.0, 0.0
    trades, wins, stops = 0, 0, 0
    curve = []
    first = max(2, ma + 1)
    for i in range(first, len(bars)):
        t, y = bars[i], bars[i - 1]
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        target = t["open"] + k * (y["high"] - y["low"])
        ret = 0.0
        trend_ok = (not ma) or t["open"] > sum(b["close"] for b in bars[i - ma:i]) / ma
        can_exit = exit == "close" or nxt is not None
        if can_exit and trend_ok and t["high"] >= target and t["open"] <= target * (1 + chase_pct / 100):
            entry = max(target, t["open"]) * (1 + slip_pct / 100)
            stop = entry * (1 - stop_pct / 100)
            if (t["close"] if optimistic else t["low"]) <= stop:
                exit_ = stop * (1 - slip_pct / 100)
                stops += 1
            elif exit == "close":
                exit_ = t["close"] * (1 - slip_pct / 100)
            else:  # 밤사이 갭하락이면 손절선보다 낮은 다음 날 시가에 나옴
                exit_ = nxt["open"] * (1 - slip_pct / 100)
            ret = exit_ / entry - 1 - fee_pct * 2 / 100 - tax_pct / 100
            trades += 1
            wins += ret > 0
        equity *= 1 + ret
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
        curve.append((t["date"], equity))
    days = len(curve)
    hold = bars[-1]["close"] / bars[first]["open"] - 1 if days else 0
    cagr = equity ** (252 / days) - 1 if days and equity > 0 else -1.0
    return {
        "기간": f"{curve[0][0]}~{curve[-1][0]}" if curve else "-",
        "거래일": days, "매매횟수": trades, "승률(%)": round(wins / trades * 100, 1) if trades else 0,
        "손절횟수": stops, "누적수익(%)": round((equity - 1) * 100, 2), "연수익(%)": round(cagr * 100, 2),
        "최대낙폭(%)": round(mdd * 100, 2), "단순보유(%)": round(hold * 100, 2),
    }


def ranged(bars, **kw):
    """최악·최선 두 가정으로 계산 → (최악 결과, 최선 결과)"""
    return backtest(bars, optimistic=False, **kw), backtest(bars, optimistic=True, **kw)


KS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
VARIANTS = [("당일청산", "close", 5), ("당일청산", "close", 20),
            ("다음날시가", "next_open", 5), ("다음날시가", "next_open", 20)]


def compare(code, bars, opts):
    """청산 방식 × 추세필터 조합마다 K를 훑어 최적 K(최악·최선 평균 기준)의 결과를 출력"""
    o = {k: v for k, v in opts.items() if k != "ma"}
    hold = backtest(bars, ma=20, **o)["단순보유(%)"]
    print(f"\n■ {code} ({len(bars)}일) — 그냥 보유 {hold:+.1f}%")
    print(f"  손절 -{o['stop_pct']}%, 수수료 편도 {o['fee_pct']}%")
    best_any = None
    for name, ex, ma in VARIANTS:
        rows = [(kk, *ranged(bars, k=kk, ma=ma, exit=ex, **o)) for kk in KS]
        kk, lo, hi = max(rows, key=lambda r: r[1]["누적수익(%)"] + r[2]["누적수익(%)"])
        print(f"  {name}/MA{ma:<2} 최적K {kk}: 수익 {lo['누적수익(%)']:+.1f}% ~ {hi['누적수익(%)']:+.1f}%"
              f" (연 {lo['연수익(%)']:+.1f}~{hi['연수익(%)']:+.1f}%), 최대낙폭 {lo['최대낙폭(%)']:.1f}%,"
              f" 매매 {lo['매매횟수']}회, 승률 {lo['승률(%)']}~{hi['승률(%)']}%")
        if best_any is None or hi["누적수익(%)"] > best_any[1]:
            best_any = (f"{name}/MA{ma} K={kk}", hi["누적수익(%)"])
    if best_any[1] < hold:
        print(f"  ⚠️ 가장 좋은 경우({best_any[0]}, {best_any[1]:+.1f}%)도 그냥 보유({hold:+.1f}%)보다 낮음")
    else:
        print(f"  ✅ {best_any[0]} 최선의 경우가 그냥 보유보다 높음 — 최악의 경우도 확인하세요")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--k", type=float, default=None)
    p.add_argument("--sweep", action="store_true", help="K 0.3~1.0 비교")
    p.add_argument("--compare", action="store_true", help="청산 방식·추세필터 조합 비교")
    p.add_argument("--exit", choices=["close", "next_open"], default=None,
                   help="close=당일 종가 청산(현재 봇), next_open=다음 날 시가 청산")
    p.add_argument("--ma", type=int, default=None, help="추세필터 이동평균 일수 덮어쓰기 (0=끔)")
    p.add_argument("--stop", type=float, default=None, help="손절(%%) 덮어쓰기")
    p.add_argument("--targets", help="종목 덮어쓰기, 예: QQQM:NAS,SOXX:NAS")
    p.add_argument("--csv", help="date,open,high,low,close 형식 CSV로 테스트")
    p.add_argument("--fee", type=float, default=None, help="편도 수수료(%%) 덮어쓰기")
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    a.exit = a.exit or g("EXIT_MODE", "close")  # 기본은 봇 설정과 같은 청산 방식
    k = a.k if a.k is not None else float(g("K", "0.5"))
    opts = dict(ma=int(g("MA_FILTER", "5")), stop_pct=float(g("STOP_LOSS_PCT", "2")),
                chase_pct=float(g("MAX_CHASE_PCT", "1")), fee_pct=float(g("FEE_PCT", "0.25")))
    if a.fee is not None:
        opts["fee_pct"] = a.fee
    if a.stop is not None:
        opts["stop_pct"] = a.stop
    if a.ma is not None:
        opts["ma"] = a.ma

    if a.csv:
        datasets = {Path(a.csv).stem: load_csv(a.csv)}
    else:
        from kis_api import KIS, parse_targets
        api = KIS(g("KIS_ENV", "mock"), g("KIS_APP_KEY"), g("KIS_APP_SECRET"), g("KIS_ACCOUNT"), HERE)
        datasets = {}
        for sym, ex in parse_targets(a.targets or g("TARGETS", "QQQM:NAS,SOXX:NAS")).items():
            print(f"{sym} 일봉 수집 중…")
            datasets[sym] = fetch_bars(api, sym, ex, a.years)

    for code, bars in datasets.items():
        if a.compare:
            compare(code, bars, opts)
            continue
        print(f"\n■ {code} ({len(bars)}일) — 청산 {'당일 종가' if a.exit == 'close' else '다음 날 시가'},"
              f" MA{opts['ma']}, 손절 -{opts['stop_pct']}%, 수수료 {opts['fee_pct']}%")
        for kk in (KS if a.sweep else [k]):
            lo, hi = ranged(bars, k=kk, exit=a.exit, **opts)
            print(f"  K={kk}: 누적수익 {lo['누적수익(%)']:+.2f}% ~ {hi['누적수익(%)']:+.2f}%,"
                  f" 연수익 {lo['연수익(%)']:+.2f}~{hi['연수익(%)']:+.2f}%, 최대낙폭 {lo['최대낙폭(%)']}%,"
                  f" 매매 {lo['매매횟수']}회, 손절 {hi['손절횟수']}~{lo['손절횟수']}회,"
                  f" 승률 {lo['승률(%)']}~{hi['승률(%)']}%, 그냥보유 {lo['단순보유(%)']:+.2f}%")
    print("\n※ 수익은 '최악 ~ 최선' 범위. 과거 성과가 미래 수익을 보장하지 않으며 슬리피지·체결 지연은 실제로 더 클 수 있습니다.")


if __name__ == "__main__":
    main()
