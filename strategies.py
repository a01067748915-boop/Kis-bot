"""
매매가 적은 전략 비교 백테스트 — 그냥 보유와 나란히 비교 (서버에서 실행)

  python strategies.py                                  # .env 의 TARGETS, 최근 5년, 대피처 없음(현금)
  python strategies.py --targets QQQM:NAS,SOXX:NAS,SCHD:AMS --safe SGOV:AMS
  python strategies.py --only trend --years 8

② 추세 추종 (종목별)
   전날 종가가 N일 이동평균 위면 보유, 아래면 팔고 현금. ±밴드를 주면 이평선 근처의 잦은 매매를 줄임
③ 모멘텀 순환 (후보 전체)
   매월 첫 거래일, 최근 N개월 수익률 1등 종목 1개만 보유. 1등도 마이너스면 대피처(--safe, 없으면 현금)

가정
  - 신호는 전날 종가로 판단, 매매는 그날 시가 ± 슬리피지, 매수·매도 때마다 편도 수수료
  - 소수점 수량 가정(1주 단위 제약 무시), 현금 이자 없음, 배당은 시세 데이터에 반영된 만큼만
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from backtest import fetch_bars, load_csv

HERE = Path(__file__).resolve().parent


# ─── 공통 시뮬레이터 ──────────────────────────────────
def simulate(dates, data, holds, fee_pct=0.25, slip_pct=0.05):
    """holds[i] = i일 시가부터 들고 있을 종목(None=현금). 바뀌는 날 시가에 팔고 삼
    → (자산곡선, 주문횟수)"""
    fee, slip = fee_pct / 100, slip_pct / 100
    eq, cur, orders, curve = 1.0, None, 0, []
    for i, d in enumerate(dates):
        tgt = holds[i]
        if tgt != cur:
            if cur is not None:  # 전날 종가 → 오늘 시가에 매도
                eq *= data[cur][d]["open"] * (1 - slip) / data[cur][dates[i - 1]]["close"] * (1 - fee)
                orders += 1
            if tgt is not None:  # 오늘 시가에 매수 → 종가까지
                b = data[tgt][d]
                eq *= b["close"] / (b["open"] * (1 + slip)) * (1 - fee)
                orders += 1
        elif cur is not None:
            eq *= data[cur][d]["close"] / data[cur][dates[i - 1]]["close"]
        cur = tgt
        curve.append(eq)
    return curve, orders


def summary(curve, days=None):
    days = days or len(curve)
    peak, mdd = 1.0, 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    end = curve[-1] if curve else 1.0
    cagr = end ** (252 / days) - 1 if days and end > 0 else -1.0
    return {"수익": (end - 1) * 100, "연": cagr * 100, "낙폭": mdd * 100}


def fmt(r, orders=None):
    s = f"수익 {r['수익']:+7.1f}% (연 {r['연']:+5.1f}%), 최대낙폭 {r['낙폭']:6.1f}%"
    return s + (f", 주문 {orders}회" if orders is not None else "")


def by_date(bars):
    return {b["date"]: b for b in bars}


# ─── ② 추세 추종 ─────────────────────────────────────
def trend_holds(bars, sym, ma, band=0.0):
    """전날 종가가 이평선×(1+밴드) 위로 오면 보유 시작, 이평선×(1-밴드) 아래로 가면 정리"""
    holds, held = [], False
    for i in range(len(bars)):
        if i < ma:
            holds.append(None)
            continue
        avg = sum(b["close"] for b in bars[i - ma:i]) / ma  # 전날까지 N일 평균
        c = bars[i - 1]["close"]
        held = c >= avg * (1 - band / 100) if held else c > avg * (1 + band / 100)
        holds.append(sym if held else None)
    return holds


TREND_VARIANTS = [(100, 0), (100, 2), (200, 0), (200, 2)]


def run_trend(datasets, opts):
    print("\n━━ ② 추세 추종: 전날 종가가 N일 이동평균 위면 보유, 아래면 현금 ━━")
    for sym, bars in datasets.items():
        start = max(m for m, _ in TREND_VARIANTS) + 1  # 모든 변형을 같은 기간으로 비교
        if len(bars) < start + 60:
            print(f"■ {sym}: 데이터 부족 ({len(bars)}일) — --years 를 늘리세요")
            continue
        dates = [b["date"] for b in bars][start:]
        data = {sym: by_date(bars)}
        hold_curve, _ = simulate(dates, data, [sym] * len(dates), **opts)
        print(f"■ {sym} {dates[0]}~{dates[-1]} ({len(dates)}일)")
        print(f"  그냥 보유      : {fmt(summary(hold_curve))}")
        for ma, band in TREND_VARIANTS:
            holds = trend_holds(bars, sym, ma, band)[start:]
            curve, orders = simulate(dates, data, holds, **opts)
            inv = sum(h is not None for h in holds) / len(holds) * 100
            label = f"MA{ma}" + (f" ±{band}%" if band else "")
            print(f"  {label:<14}: {fmt(summary(curve), orders)}, 보유기간 {inv:.0f}%")


# ─── ③ 모멘텀 순환 ───────────────────────────────────
def rotation_holds(dates, data, cands, safe, lookback):
    """매월 첫 거래일에만 교체. 최근 lookback 거래일 수익률 1등, 1등도 0 이하면 대피처(없으면 현금)"""
    holds, cur = [], None
    for i, d in enumerate(dates):
        if i > lookback and d[:6] != dates[i - 1][:6]:
            prev, base = dates[i - 1], dates[i - 1 - lookback]
            score = {s: data[s][prev]["close"] / data[s][base]["close"] - 1 for s in cands}
            best = max(score, key=score.get)
            cur = best if score[best] > 0 else safe
        holds.append(cur)
    return holds


LOOKBACKS = [(3, 63), (6, 126), (12, 252)]


def run_rotation(datasets, safe, opts):
    cands = [s for s in datasets if s != safe]
    print(f"\n━━ ③ 모멘텀 순환: 매월 첫 거래일, 최근 N개월 수익률 1등만 보유"
          f" / 1등도 마이너스면 {safe or '현금'} ━━")
    if len(cands) < 2:
        print("  후보가 2개 이상 필요합니다 (--targets)")
        return
    common = sorted(set.intersection(*(set(b["date"] for b in bars) for bars in datasets.values())))
    start = max(lb for _, lb in LOOKBACKS) + 1
    if len(common) < start + 60:
        print(f"  공통 데이터 부족 ({len(common)}일) — --years 를 늘리세요")
        return
    data = {s: by_date(b) for s, b in datasets.items()}
    dates = common[start:]
    print(f"  후보 {', '.join(cands)} / 기간 {dates[0]}~{dates[-1]} ({len(dates)}일)")
    curves = {}
    for s in cands:
        curves[s], _ = simulate(dates, data, [s] * len(dates), **opts)
        print(f"  {s + ' 보유':<14}: {fmt(summary(curves[s]))}")
    equal = [sum(c[i] for c in curves.values()) / len(cands) for i in range(len(dates))]
    print(f"  {'균등 보유':<12}: {fmt(summary(equal))}")
    for months, lb in LOOKBACKS:
        holds = rotation_holds(common, data, cands, safe, lb)[start:]
        curve, orders = simulate(dates, data, holds, **opts)
        share = {}
        for h in holds:
            share[h or "현금"] = share.get(h or "현금", 0) + 1
        mix = " ".join(f"{k} {v / len(holds) * 100:.0f}%" for k, v in sorted(share.items(), key=lambda x: -x[1]))
        print(f"  {f'{months}개월 모멘텀':<12}: {fmt(summary(curve), orders)} | 보유비중 {mix}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--targets", help="종목, 예: QQQM:NAS,SOXX:NAS,SCHD:AMS (기본: .env 의 TARGETS)")
    p.add_argument("--safe", help="③ 대피처 종목, 예: SGOV:AMS (없으면 현금)")
    p.add_argument("--only", choices=["trend", "rotation"], help="한 전략만 실행")
    p.add_argument("--fee", type=float, default=None, help="편도 수수료(%%) 덮어쓰기")
    p.add_argument("--csv", nargs="+", help="date,open,high,low,close CSV 파일들로 테스트 (파일명=종목명)")
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    opts = {"fee_pct": a.fee if a.fee is not None else float(g("FEE_PCT", "0.25")), "slip_pct": 0.05}
    safe = None
    if a.csv:
        datasets = {Path(f).stem: load_csv(f) for f in a.csv}
    else:
        from kis_api import KIS, parse_targets
        api = KIS(g("KIS_ENV", "mock"), g("KIS_APP_KEY"), g("KIS_APP_SECRET"), g("KIS_ACCOUNT"), HERE)
        targets = parse_targets(a.targets or g("TARGETS", "QQQM:NAS,SOXX:NAS"))
        if a.safe:
            extra = parse_targets(a.safe)
            safe = next(iter(extra))
            targets.update(extra)
        datasets = {}
        for sym, ex in targets.items():
            print(f"{sym} 일봉 수집 중…")
            datasets[sym] = fetch_bars(api, sym, ex, a.years)
            if not datasets[sym]:
                print(f"  ⚠️ {sym}({ex}) 시세 없음 — 거래소 코드를 확인하세요")
                datasets.pop(sym)

    print(f"\n수수료 편도 {opts['fee_pct']}%, 슬리피지 {opts['slip_pct']}%")
    if a.only != "rotation":
        run_trend({s: b for s, b in datasets.items() if s != safe}, opts)
    if a.only != "trend":
        run_rotation(datasets, safe, opts)
    print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다. 최대낙폭은 '가장 많이 빠졌을 때'라 함께 보세요.")


if __name__ == "__main__":
    main()
