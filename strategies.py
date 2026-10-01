"""
매매가 적은 전략 비교 백테스트 — 그냥 보유와 나란히 비교 (서버에서 실행)

  python strategies.py                                  # .env 의 TARGETS, 최근 5년, 대피처 없음(현금)
  python strategies.py --targets QQQM:NAS,SOXX:NAS,SCHD:AMS --safe SGOV:AMS
  python strategies.py --only trend --years 8
  python strategies.py --only pullback --targets SOXX:NAS,QQQM:NAS,AMD:NAS,PLTR:NAS
  python strategies.py --only portfolio --targets SOXX:NAS,SMH:NAS,AMD:NAS,PLTR:NAS,HOOD:NAS

A. 눌림목 매수 (종목별, 며칠 보유)
   200일선 위(상승 추세)인데 RSI(2)가 기준 아래로 급락하면 다음 날 시가에 매수
   → 종가가 5일선 위로 회복하면 다음 날 시가에 매도 (최대 보유 10일, 선택: 손절)

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


def usd(x):
    return f"${x:,.0f}"


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


# ─── A. 눌림목 매수 ───────────────────────────────────
def rsi(closes, n=2):
    """와일더 방식 RSI. 앞쪽 n개는 None"""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def pullback(bars, rsi_max=10, trend_ma=200, exit_ma=5, max_days=10, stop_pct=None, fee_pct=0.25, slip_pct=0.05):
    """눌림목 매수 1종목 시뮬레이션 → (자산곡선, 거래목록[(수익률, 보유일, 진입 인덱스)], 시작 인덱스)
    수익률은 매수·매도 수수료와 슬리피지를 모두 뺀 값
    신호는 전날 종가 기준, 매매는 시가. 손절은 장중 저가가 손절선 아래면 손절가에 체결(불리하게)"""
    fee, slip = fee_pct / 100, slip_pct / 100
    closes = [b["close"] for b in bars]
    r = rsi(closes, 2)
    start = max(trend_ma, exit_ma, 3)
    eq, curve, trades = 1.0, [], []
    pos = None  # entry: 체결가, i: 진입일, base: 매수 수수료를 뗀 투입 자산

    def close_out(px, days):
        nonlocal eq, pos
        start_eq = pos["base"] / (1 - fee)
        eq = pos["base"] * px / pos["entry"] * (1 - fee)
        trades.append((eq / start_eq - 1, days, pos["i"]))
        pos = None

    for i in range(start, len(bars)):
        b, prev = bars[i], bars[i - 1]
        if pos is None:
            trend_ok = not trend_ma or prev["close"] > sum(closes[i - trend_ma:i]) / trend_ma
            if trend_ok and r[i - 1] is not None and r[i - 1] < rsi_max:
                entry = b["open"] * (1 + slip)
                pos = {"entry": entry, "i": i, "base": eq * (1 - fee)}
        else:
            held = i - pos["i"]
            recovered = prev["close"] > sum(closes[i - exit_ma:i]) / exit_ma
            if recovered or held >= max_days:  # 전날 신호 → 오늘 시가에 매도
                close_out(b["open"] * (1 - slip), held)
        if pos is not None:
            stop = pos["entry"] * (1 - stop_pct / 100) if stop_pct else None
            if stop and b["low"] <= stop:
                close_out(min(stop, b["open"]) * (1 - slip), i - pos["i"] + 1)  # 갭하락이면 시가에 체결
            else:
                eq = pos["base"] * b["close"] / pos["entry"]
        curve.append(eq)
    return curve, trades, start


PULLBACK_VARIANTS = [  # (이름, rsi_max, trend_ma, stop_pct)
    ("RSI<5", 5, 200, None), ("RSI<10", 10, 200, None), ("RSI<20", 20, 200, None),
    ("RSI<10 손절8%", 10, 200, 8), ("RSI<10 추세무시", 10, 0, None),
]


def run_pullback(datasets, opts):
    print("\n━━ A. 눌림목 매수: 200일선 위 + RSI(2) 급락 → 매수, 5일선 회복 → 매도 (최대 10일) ━━")
    print("  ※ 1회 평균수익이 비용(약 0.6%)보다 확실히 커야 실전에서 의미 있음")
    total = {name: [] for name, *_ in PULLBACK_VARIANTS}
    for sym, bars in datasets.items():
        if len(bars) < 260:
            print(f"■ {sym}: 데이터 부족 ({len(bars)}일) — --years 를 늘리세요")
            continue
        start = 200
        dates = [b["date"] for b in bars][start:]
        hold_curve, _ = simulate(dates, {sym: by_date(bars)}, [sym] * len(dates), **opts)
        print(f"■ {sym} {dates[0]}~{dates[-1]} ({len(dates)}일)")
        print(f"  그냥 보유       : {fmt(summary(hold_curve))}")
        for name, rmax, tma, stop in PULLBACK_VARIANTS:
            curve, trades, st = pullback(bars, rsi_max=rmax, trend_ma=tma, stop_pct=stop, **opts)
            curve = curve[start - st:]  # 추세무시 변형도 같은 기간으로 비교
            trades_in = [(t, d) for t, d, i in trades if i >= start]
            total[name] += trades_in
            n = len(trades_in)
            if not n:
                print(f"  {name:<12}: 매매 없음")
                continue
            avg = sum(t for t, _ in trades_in) / n * 100
            win = sum(t > 0 for t, _ in trades_in) / n * 100
            days = sum(d for _, d in trades_in) / n
            print(f"  {name:<12}: {fmt(summary(curve))}, 매매 {n}회, 승률 {win:.0f}%,"
                  f" 1회평균 {avg:+.2f}%, 평균보유 {days:.1f}일")
    print("■ 전체 종목 합산 (1회 평균수익이 핵심)")
    for name, trades in total.items():
        n = len(trades)
        if n:
            avg = sum(t for t, _ in trades) / n * 100
            win = sum(t > 0 for t, _ in trades) / n * 100
            mark = "✅" if avg > 0.5 else ("△" if avg > 0 else "⚠️")
            print(f"  {mark} {name:<12}: 매매 {n}회, 승률 {win:.0f}%, 1회평균 {avg:+.2f}% (수수료·슬리피지 차감 후)")


# ─── A'. 눌림목 매수 — 예산을 모아 쓰는 포트폴리오 ─────
def indicators(bars, trend_ma=200, exit_ma=5):
    """날짜별 {close, rsi, trend(종가>추세선), recovered(종가>단기선)} — 그날 종가까지로 계산"""
    closes = [b["close"] for b in bars]
    r = rsi(closes, 2)
    out = {}
    for i, b in enumerate(bars):
        if i + 1 < max(trend_ma, exit_ma) or r[i] is None:
            continue
        trend = not trend_ma or closes[i] > sum(closes[i + 1 - trend_ma:i + 1]) / trend_ma
        out[b["date"]] = {"rsi": r[i], "trend": trend,
                          "recovered": closes[i] > sum(closes[i + 1 - exit_ma:i + 1]) / exit_ma}
    return out


def portfolio(datasets, budget=950.0, slots=2, rsi_max=10, trend_ma=200, max_days=10,
              fee_pct=0.25, slip_pct=0.05, whole_shares=True):
    """예산 하나를 slots 칸으로 나눠, 신호 난 종목 중 RSI가 가장 낮은 것부터 채움
    → {"dates", "curve"(달러), "trades"[(수익률, 보유일)], "used"(평균 사용 칸 비율)}"""
    fee, slip = fee_pct / 100, slip_pct / 100
    ind = {s: indicators(b, trend_ma) for s, b in datasets.items()}
    px = {s: by_date(b) for s, b in datasets.items()}
    common = sorted(set.intersection(*(set(v) for v in px.values())))
    prev_of = dict(zip(common[1:], common))
    dates = [d for d in common[1:] if all(prev_of[d] in ind[s] for s in ind)]  # 지표가 다 준비된 날부터
    cash, pos, curve, trades, used = budget, {}, [], [], 0
    for d in dates:
        y = prev_of[d]
        # 1) 매도: 전날 종가가 단기선 위로 회복했거나 최대 보유일 도달 → 오늘 시가
        for s in list(pos):
            p = pos[s]
            p["days"] += 1
            if ind[s][y]["recovered"] or p["days"] >= max_days:
                got = p["qty"] * px[s][d]["open"] * (1 - slip) * (1 - fee)
                cash += got
                trades.append((got / p["cost"] - 1, p["days"]))
                del pos[s]
        # 2) 매수: 빈 칸만큼, 전날 RSI 낮은 순
        equity_open = cash + sum(p["qty"] * px[s][d]["open"] for s, p in pos.items())
        cands = sorted((ind[s][y]["rsi"], s) for s in ind
                       if s not in pos and ind[s][y]["trend"] and ind[s][y]["rsi"] < rsi_max)
        for _, s in cands:
            if len(pos) >= slots:
                break
            price = px[s][d]["open"] * (1 + slip)
            alloc = min(equity_open / slots, cash)
            qty = int(alloc * (1 - fee) / price) if whole_shares else alloc * (1 - fee) / price
            if qty <= 0:
                continue  # 1주도 못 사면 다음 후보
            cost = qty * price / (1 - fee)
            cash -= cost
            pos[s] = {"qty": qty, "cost": cost, "days": 0}
        used += len(pos)
        curve.append(cash + sum(p["qty"] * px[s][d]["close"] for s, p in pos.items()))
    return {"dates": dates, "curve": curve, "trades": trades, "used": used / max(1, len(dates)) / slots}


PORTFOLIO_VARIANTS = [  # (이름, slots, rsi_max)
    ("RSI<10 1칸", 1, 10), ("RSI<10 2칸", 2, 10), ("RSI<10 3칸", 3, 10),
    ("RSI<5  2칸", 2, 5), ("RSI<20 3칸", 3, 20),
]


def yearly(dates, curve, start_value):
    out, last, prev_end = [], None, start_value
    for d, v in zip(dates, curve):
        if last and d[:4] != last[0]:
            out.append((last[0], last[1] / prev_end - 1))
            prev_end = last[1]
        last = (d[:4], v)
    if last:
        out.append((last[0], last[1] / prev_end - 1))
    return " ".join(f"{yr} {r * 100:+.0f}%" for yr, r in out)


def run_portfolio(datasets, opts, budget):
    print(f"\n━━ A'. 눌림목 매수 — 예산 {usd(budget)} 하나로 모든 종목 감시, 신호 난 종목에 칸 단위로 투입 ━━")
    print(f"  종목 {', '.join(datasets)} / 같은 날 여러 신호면 RSI 낮은 순 / 1주 단위 매수")
    if len(datasets) < 2:
        print("  종목이 2개 이상 필요합니다")
        return
    base = portfolio(datasets, budget, **opts)
    dates = base["dates"]
    if len(dates) < 60:
        print(f"  공통 데이터 부족 ({len(dates)}일) — --years 를 늘리세요")
        return
    hold = [simulate(dates, {s: by_date(b)}, [s] * len(dates), **opts)[0] for s, b in datasets.items()]
    eq_hold = [sum(c[i] for c in hold) / len(hold) * budget for i in range(len(dates))]
    print(f"  기간 {dates[0]}~{dates[-1]} ({len(dates)}일)")
    print(f"  균등 보유     : {fmt(summary([v / budget for v in eq_hold]))}")
    print(f"     연도별   : {yearly(dates, eq_hold, budget)}")
    for name, slots, rmax in PORTFOLIO_VARIANTS:
        r = portfolio(datasets, budget, slots=slots, rsi_max=rmax, **opts)
        t = r["trades"]
        n = len(t)
        avg = sum(x for x, _ in t) / n * 100 if n else 0
        win = sum(x > 0 for x, _ in t) / n * 100 if n else 0
        print(f"  {name:<11}: {fmt(summary([v / budget for v in r['curve']]))}, 매매 {n}회,"
              f" 승률 {win:.0f}%, 1회평균 {avg:+.2f}%, 투자비율 {r['used'] * 100:.0f}%")
        print(f"     연도별   : {yearly(r['dates'], r['curve'], budget)}  → 최종 {usd(r['curve'][-1])}")


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
    p.add_argument("--only", choices=["pullback", "portfolio", "trend", "rotation"], help="한 전략만 실행")
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
    stocks = {s: b for s, b in datasets.items() if s != safe}
    if a.only in (None, "pullback"):
        run_pullback(stocks, opts)
    if a.only in (None, "portfolio"):
        run_portfolio(stocks, opts, float(g("BUDGET_USD", "950")))
    if a.only in (None, "trend"):
        run_trend(stocks, opts)
    if a.only in (None, "rotation"):
        run_rotation(datasets, safe, opts)
    print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다. 최대낙폭은 '가장 많이 빠졌을 때'라 함께 보세요.")


if __name__ == "__main__":
    main()
