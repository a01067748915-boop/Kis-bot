"""
매매가 적은 전략 비교 백테스트 — 그냥 보유와 나란히 비교 (서버에서 실행)

  python strategies.py                                  # .env 의 TARGETS, 최근 5년, 대피처 없음(현금)
  python strategies.py --targets QQQM:NAS,SOXX:NAS,SCHD:AMS --safe SGOV:AMS
  python strategies.py --only trend --years 8
  python strategies.py --only pullback --targets SOXX:NAS,QQQM:NAS,AMD:NAS,PLTR:NAS
  python strategies.py --only portfolio --targets SOXX:NAS,SMH:NAS,AMD:NAS,PLTR:NAS,HOOD:NAS
  python strategies.py --only breakout --targets @universes/growth_balanced.txt --years 10
  python strategies.py --only robust --targets @universes/growth_balanced.txt --add @universes/growth_extra.txt --years 10

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
from signals import breakout_indicators, indicators, market_indicators, rsi

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


def warn_jumps(sym, bars, limit=0.45):
    """하루에 -45% 이하나 +80% 이상 움직인 날 — 실제 폭락일 수도, 분할 미반영 데이터일 수도 있어 표시"""
    jumps = [(b["date"], b["close"] / a["close"] - 1) for a, b in zip(bars, bars[1:])
             if a["close"] > 0 and not (1 - limit < b["close"] / a["close"] < 1 / (1 - limit))]
    if jumps:
        print(f"  ⚠️ {sym} 하루 급변 {len(jumps)}회: "
              + ", ".join(f"{d} {r * 100:+.0f}%" for d, r in jumps[:4]) + " — 분할 미반영인지 확인 필요")
    return jumps


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
def slot_engine(datasets, ind, budget, slots, wants_exit, candidates, fee_pct=0.25, slip_pct=0.05,
                whole_shares=True, start=None, date_ok=None, raw=None):
    """예산 하나를 slots 칸으로 나눠 운용하는 공통 엔진 (매매는 모두 그날 시가, 신호는 전날 종가 기준)
    ind[s][날짜]: 종목별 지표 / wants_exit(s, y, pos) → 매도 사유 여부 / candidates(y, d, ready, held) → 매수 우선순위 목록
    date_ok(y) → 그날 진행 여부(시장 데이터 등) / 1주 단위 매수, 칸 예산으로 1주도 못 사면 다음 후보
    raw: {종목: 실제 가격 일봉} 을 주면 '그날 실제 가격으로 몇 주 살 수 있었나'로 수량을 정함
         (수익률은 수정주가로 계산 — 분할일의 가격 급변을 손익으로 착각하지 않게)"""
    fee, slip = fee_pct / 100, slip_pct / 100
    px = {s: by_date(b) for s, b in datasets.items()}
    cal = sorted(set().union(*(set(v) for v in px.values())))
    prev_of = dict(zip(cal[1:], cal))

    def ready(s, d):  # 오늘 시세가 있고 전날 지표가 준비됨
        return d in px[s] and prev_of.get(d) in ind[s]

    if start is None:
        start = next((d for d in cal[1:] if all(ready(s, d) for s in px)), None)
    dates = [d for d in cal[1:] if start and d >= start and (date_ok is None or date_ok(prev_of[d]))]
    cash, pos, curve, trades, used, last, skipped, skips = budget, {}, [], [], 0, {}, set(), 0
    rawpx = {s: by_date(b) for s, b in raw.items()} if raw else None
    for d in dates:
        y = prev_of[d]
        # 1) 매도 (거래정지 등 시세 없는 날은 보류)
        for s in list(pos):
            p = pos[s]
            p["days"] += 1
            if ready(s, d) and wants_exit(s, y, p):
                got = p["qty"] * px[s][d]["open"] * (1 - slip) * (1 - fee)
                cash += got
                trades.append((got / p["cost"] - 1, p["days"], s))
                del pos[s]
        # 2) 매수: 빈 칸만큼 우선순위대로
        equity_open = cash + sum(p["qty"] * (px[s][d]["open"] if d in px[s] else last[s]) for s, p in pos.items())
        for s in candidates(y, d, ready, pos):
            if len(pos) >= slots:
                break
            price = px[s][d]["open"] * (1 + slip)
            alloc = min(equity_open / slots, cash)
            if rawpx is not None:
                rb = rawpx.get(s, {}).get(d)
                if not rb:
                    continue
                real = rb["open"] * (1 + slip)
                n = int(alloc * (1 - fee) / real)
                qty = n * real / price  # 실제로 산 주식 수를 수정주가 단위로 환산
            else:
                n = qty = int(alloc * (1 - fee) / price) if whole_shares else alloc * (1 - fee) / price
            if n <= 0:
                skipped.add(s)
                skips += 1
                continue
            cost = qty * price / (1 - fee)
            cash -= cost
            pos[s] = {"qty": qty, "cost": cost, "days": 0}
        used += len(pos)
        for s in px:
            if d in px[s]:
                last[s] = px[s][d]["close"]
        curve.append(cash + sum(p["qty"] * last[s] for s, p in pos.items()))
    n = max(1, len(dates))
    return {"dates": dates, "curve": curve, "trades": trades, "used": used / n / slots, "open": len(pos),
            "skipped": skipped - {t[2] for t in trades}, "skips": skips}


def portfolio(datasets, budget=950.0, slots=2, rsi_max=10, trend_ma=200, max_days=10,
              fee_pct=0.25, slip_pct=0.05, whole_shares=True, market=None, mfilter=None, mrsi_max=10,
              start=None):
    """눌림목 매수 포트폴리오: 신호 난 종목 중 RSI가 가장 낮은 것부터 칸을 채움
    market: market_indicators() 결과(날짜별), mfilter: 시장 필터
      "trend" 시장이 200일선 위일 때만 매수 / "panic" 최근 3일 안에 투매(하락+거래량 급증)가 있을 때만
      "mrsi"  시장 RSI(2) < mrsi_max 일 때만, 종목 RSI 기준 없이 200일선 위 종목 중 RSI 낮은 순
    start: 시작일(YYYYMMDD). 없으면 모든 종목 지표가 준비된 첫날. 시작 뒤 상장·지표 준비된 종목은 그때부터 참여
    → {"dates", "curve"(달러), "trades"[(수익률, 보유일, 종목)], "used"(평균 사용 칸 비율),
       "open_days"(매수 허용일 비율), "skipped"(신호는 났지만 1주 가격이 칸보다 비싸 못 산 종목)}"""
    ind = {s: indicators(b, trend_ma) for s, b in datasets.items()}
    limit = 101 if mfilter == "mrsi" else rsi_max
    allowed = []

    def wants_exit(s, y, p):  # 전날 종가가 단기선 위로 회복했거나 최대 보유일 도달
        return ind[s][y]["recovered"] or p["days"] >= max_days

    def candidates(y, d, ready, held):  # 전날 RSI 낮은 순 (시장 필터가 막으면 쉼)
        m = market[y] if market is not None else None
        allow = (mfilter is None or (mfilter == "trend" and m["trend"])
                 or (mfilter == "panic" and m["panic_recent"]) or (mfilter == "mrsi" and m["rsi"] < mrsi_max))
        allowed.append(allow)
        if not allow:
            return []
        return [s for _, s in sorted((ind[s][y]["rsi"], s) for s in ind if s not in held and ready(s, d)
                                     and ind[s][y]["trend"] and ind[s][y]["rsi"] < limit)]

    r = slot_engine(datasets, ind, budget, slots, wants_exit, candidates, fee_pct, slip_pct, whole_shares, start,
                    None if market is None else (lambda y: y in market))
    r["open_days"] = sum(allowed) / max(1, len(allowed))
    return r


def breakout_portfolio(datasets, budget=950.0, slots=2, entry_n=55, exit_n=20, trend_ma=200, mom_n=126,
                       fee_pct=0.25, slip_pct=0.05, whole_shares=True, start=None):
    """B. 스윙 돌파(터틀 방식): 전날 종가가 직전 entry_n일 최고 종가를 넘으면 매수,
    직전 exit_n일 최저 종가 아래로 내려가면 매도. 같은 날 여러 신호면 최근 mom_n일 수익률 높은 순"""
    ind = {s: breakout_indicators(b, entry_n, exit_n, trend_ma, mom_n) for s, b in datasets.items()}

    def wants_exit(s, y, p):
        return ind[s][y]["exit"]

    def candidates(y, d, ready, held):
        return [s for _, s in sorted((-ind[s][y]["mom"], s) for s in ind if s not in held and ready(s, d)
                                     and ind[s][y]["entry"] and ind[s][y]["trend"])]

    return slot_engine(datasets, ind, budget, slots, wants_exit, candidates, fee_pct, slip_pct, whole_shares, start)


PORTFOLIO_VARIANTS = [  # (이름, slots, rsi_max)
    ("RSI<10 1칸", 1, 10), ("RSI<10 2칸", 2, 10), ("RSI<10 3칸", 3, 10), ("RSI<10 4칸", 4, 10),
    ("RSI<5  2칸", 2, 5), ("RSI<5  4칸", 4, 5), ("RSI<20 3칸", 3, 20),
]


def common_start(datasets, trend_ma=200, share=0.75):
    """종목의 share 비율 이상이 지표 준비된 첫날 — 늦게 상장한 소수 종목 때문에 기간이 줄지 않게"""
    ready = sorted(min(indicators(b, trend_ma) or {"99999999": 0}) for b in datasets.values())
    k = max(1, int(len(ready) * share + 0.999)) - 1
    return ready[k] if ready[k] != "99999999" else None


def hold_curve(datasets, dates, budget, fee_pct=0.25, slip_pct=0.05):
    """시작일에 시세가 있는 종목을 균등하게 사서 보유(빈 날은 직전 종가) → (곡선, 포함 종목 수)"""
    fee, slip = fee_pct / 100, slip_pct / 100
    pxs = {s: by_date(b) for s, b in datasets.items()}
    names = [s for s, p in pxs.items() if dates[0] in p]
    shares = {s: budget / len(names) * (1 - fee) / (pxs[s][dates[0]]["open"] * (1 + slip)) for s in names}
    last, curve = {}, []
    for d in dates:
        for s in names:
            if d in pxs[s]:
                last[s] = pxs[s][d]["close"]
        curve.append(sum(shares[s] * last[s] for s in names if s in last))
    return curve, len(names)


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


def print_portfolio_row(name, r, budget):
    t = r["trades"]
    n = len(t)
    avg = sum(x[0] for x in t) / n * 100 if n else 0
    win = sum(x[0] > 0 for x in t) / n * 100 if n else 0
    days = sum(x[1] for x in t) / n if n else 0
    print(f"  {name:<11}: {fmt(summary([v / budget for v in r['curve']]))}, 매매 {n}회,"
          f" 승률 {win:.0f}%, 1회평균 {avg:+.2f}%, 평균보유 {days:.0f}일, 투자비율 {r['used'] * 100:.0f}%")
    print(f"     연도별   : {yearly(r['dates'], r['curve'], budget)}  → 최종 {usd(r['curve'][-1])}")


MARKET_FILTERS = [  # (이름, mfilter)
    ("필터 없음", None), ("①시장추세", "trend"), ("②투매", "panic"), ("③시장과매도", "mrsi"),
]


def run_market_filters(datasets, opts, budget, market_name, market_bars, slots=2):
    print(f"\n━━ 시장 필터 비교 ({market_name} 기준, RSI<10 {slots}칸) ━━")
    print(f"  ①시장추세   : {market_name} 가 200일선 위일 때만 매수")
    print(f"  ②투매       : 최근 3거래일 안에 {market_name} 하락 + 거래량 20일 평균의 1.5배↑ 인 날이 있을 때만")
    print(f"  ③시장과매도 : {market_name} RSI(2)<10 일 때만, 종목 RSI 기준 없이 200일선 위 종목 중 많이 빠진 순")
    m = market_indicators(market_bars)
    if not m:
        print(f"  {market_name} 데이터 부족 — --years 를 늘리세요")
        return
    if not any(b.get("volume") for b in market_bars):
        print("  ⚠️ 거래량 데이터 없음 → ②투매 결과는 의미 없음")
    start = common_start(datasets)
    for name, f in MARKET_FILTERS:
        r = portfolio(datasets, budget, slots=slots, market=m, mfilter=f, start=start, **opts)
        print_portfolio_row(name, r, budget)
        if f:
            print(f"     매수 허용일 {r['open_days'] * 100:.0f}%")


UNIVERSE_VARIANTS = [  # (이름, slots, rsi_max)
    ("2칸 RSI<10", 2, 10), ("3칸 RSI<10", 3, 10), ("4칸 RSI<10", 4, 10), ("3칸 RSI<5", 3, 5),
]


def run_universe(base, extra, opts, budget):
    """기존 종목 vs 종목 추가 — 같은 시작일로 비교"""
    allset = {**base, **extra}
    ref = portfolio(base, budget, slots=2, **opts)
    if not ref["dates"]:
        print("  기존 종목 데이터 부족")
        return
    start = ref["dates"][0]
    print(f"\n━━ 종목 수 비교: 기존 {len(base)}개 vs 추가 후 {len(allset)}개 (같은 시작일 {start}) ━━")
    print(f"  추가: {', '.join(extra)}")
    late = [s for s, b in extra.items() if b and b[0]["date"] > start]
    if late:
        print(f"  ※ 시작일 뒤 상장·데이터 시작이라 중간부터 참여: {', '.join(late)}")
    print_portfolio_row(f"기존{len(base)} 2칸", ref, budget)
    for name, slots, rmax in UNIVERSE_VARIANTS:
        r = portfolio(allset, budget, slots=slots, rsi_max=rmax, start=start, **opts)
        print_portfolio_row(f"{len(allset)}개 {name}", r, budget)
        if r["skipped"]:
            print(f"     칸 예산보다 비싸 한 번도 못 산 종목: {', '.join(sorted(r['skipped']))}")
    best = portfolio(allset, budget, slots=3, rsi_max=10, start=start, **opts)
    by_sym = {}
    for ret, _, sym in best["trades"]:
        n, tot = by_sym.get(sym, (0, 0.0))
        by_sym[sym] = (n + 1, tot + ret)
    if by_sym:
        ranked = sorted(by_sym.items(), key=lambda x: -x[1][1])
        print("  종목별 기여 (3칸 RSI<10, 누적 수익률 합): "
              + ", ".join(f"{k} {t * 100:+.0f}%({n})" for k, (n, t) in ranked))


BREAKOUT_VARIANTS = [  # (이름, slots, entry_n, exit_n, trend_ma)
    ("돌파20/10 2칸", 2, 20, 10, 200), ("돌파20/10 4칸", 4, 20, 10, 200),
    ("돌파55/20 2칸", 2, 55, 20, 200), ("돌파55/20 4칸", 4, 55, 20, 200),
    ("돌파55/20 4칸 추세무시", 4, 55, 20, 0),
]


def run_breakout(datasets, opts, budget):
    print(f"\n━━ B. 스윙 돌파 — 예산 {usd(budget)}, N일 최고가 돌파 매수 · M일 최저가 이탈 매도 ━━")
    print("  같은 날 여러 신호면 최근 6개월 수익률 높은 순 / 200일선 위만 (추세무시 제외) / 1주 단위 매수")
    if len(datasets) < 2:
        print("  종목이 2개 이상 필요합니다")
        return
    start = common_start(datasets)
    ref = portfolio(datasets, budget, slots=4, rsi_max=5, start=start, **opts)
    dates = ref["dates"]
    if len(dates) < 60:
        print(f"  공통 데이터 부족 ({len(dates)}일) — --years 를 늘리세요")
        return
    eq_hold, n_hold = hold_curve(datasets, dates, budget, **opts)
    print(f"  기간 {dates[0]}~{dates[-1]} ({len(dates)}일)")
    print(f"  균등 보유({n_hold}) : {fmt(summary([v / budget for v in eq_hold]))}")
    print(f"     연도별   : {yearly(dates, eq_hold, budget)}")
    print_portfolio_row("(비교) 눌림목 RSI<5 4칸", ref, budget)
    for name, slots, en, ex, tma in BREAKOUT_VARIANTS:
        r = breakout_portfolio(datasets, budget, slots=slots, entry_n=en, exit_n=ex, trend_ma=tma, start=start, **opts)
        print_portfolio_row(name, r, budget)
        if r["skipped"]:
            print(f"     칸 예산보다 비싸 한 번도 못 산 종목: {', '.join(sorted(r['skipped']))}")


GRID_ENTRY = [10, 15, 20, 30, 40, 55]
GRID_EXIT = [5, 7, 10, 15, 20]


def _row(r, budget):
    t = r["trades"]
    n = len(t)
    sm = summary([v / budget for v in r["curve"]])
    avg = sum(x[0] for x in t) / n * 100 if n else 0
    return sm, n, avg


def run_robust(base, extra, opts, budget, slots=4):
    """스윙 돌파 검증 3가지: ① 종목 묶음 바꿔도 이기는지 ② 설정값 근처도 비슷한지 ③ 최근에도 통하는지"""
    allset = {**base, **extra}
    start = common_start(allset)
    print(f"\n━━ 스윙 돌파 검증 — 예산 {usd(budget)}, 기본 {slots}칸, 시작일 {start} ━━")

    # ① 종목 묶음
    print("\n① 종목 묶음별 (같은 시작일)")
    groups = [(f"기존 {len(base)}종목", base)]
    if extra:
        groups += [(f"추가 {len(extra)}종목만", extra), (f"합계 {len(allset)}종목", allset)]
    for gname, ds in groups:
        ref = breakout_portfolio(ds, budget, slots=slots, entry_n=20, exit_n=10, start=start, **opts)
        if not ref["dates"]:
            print(f"  {gname}: 데이터 부족")
            continue
        hold, n_hold = hold_curve(ds, ref["dates"], budget, **opts)
        print(f"  ■ {gname} — 균등 보유({n_hold}): {fmt(summary([v / budget for v in hold]))}")
        for name, sl, en, ex in [("20/10", 2, 20, 10), ("20/10", slots, 20, 10), ("55/20", slots, 55, 20)]:
            r = ref if (sl, en, ex) == (slots, 20, 10) else breakout_portfolio(
                ds, budget, slots=sl, entry_n=en, exit_n=ex, start=start, **opts)
            sm, n, avg = _row(r, budget)
            print(f"    돌파{name} {sl}칸: {fmt(sm)}, 매매 {n}회, 1회평균 {avg:+.2f}% → 최종 {usd(r['curve'][-1])}")

    # ② 설정값 그리드
    ds = allset
    print(f"\n② 설정값 그리드 ({len(ds)}종목, {slots}칸) — 칸마다 '연수익/최대낙폭' (%)")
    print("  진입\\청산 " + "".join(f"{x:>4}일     " for x in GRID_EXIT))
    for en in GRID_ENTRY:
        cells = []
        for ex in GRID_EXIT:
            if ex >= en:
                cells.append("    -      ")
                continue
            sm, n, _ = _row(breakout_portfolio(ds, budget, slots=slots, entry_n=en, exit_n=ex, start=start, **opts),
                            budget)
            cells.append(f"{sm['연']:+4.0f}/{sm['낙폭']:4.0f}  ")
        print(f"  {en:>3}일    " + "".join(cells))
    print("  → 한 칸만 튀지 않고 주변 칸도 고르게 좋으면 우연이 아닐 가능성이 큼")

    # ③ 기간별
    print(f"\n③ 기간별 ({len(ds)}종목) — 같은 규칙을 최근 구간에서 새로 시작했을 때")
    full = breakout_portfolio(ds, budget, slots=slots, entry_n=20, exit_n=10, start=start, **opts)
    if not full["dates"]:
        print("  데이터 부족")
        return
    end = full["dates"][-1]
    for label, years in [("전체", None), ("최근 4년", 4), ("최근 2년", 2), ("최근 1년", 1)]:
        cut = start if years is None else f"{int(end[:4]) - years}{end[4:]}"
        sub = [d for d in full["dates"] if d >= cut]
        if len(sub) < 60:
            continue
        hold, _ = hold_curve(ds, sub, budget, **opts)
        print(f"  ■ {label} ({sub[0]}~) 균등 보유: {fmt(summary([v / budget for v in hold]))}")
        for sl, en, ex in [(2, 20, 10), (slots, 20, 10), (slots, 55, 20)]:
            sm, n, avg = _row(breakout_portfolio(ds, budget, slots=sl, entry_n=en, exit_n=ex, start=sub[0], **opts),
                              budget)
            print(f"    돌파{en}/{ex} {sl}칸: {fmt(sm)}, 매매 {n}회, 1회평균 {avg:+.2f}%")


def run_portfolio(datasets, opts, budget):
    print(f"\n━━ A'. 눌림목 매수 — 예산 {usd(budget)} 하나로 모든 종목 감시, 신호 난 종목에 칸 단위로 투입 ━━")
    print(f"  종목 {', '.join(datasets)} / 같은 날 여러 신호면 RSI 낮은 순 / 1주 단위 매수")
    if len(datasets) < 2:
        print("  종목이 2개 이상 필요합니다")
        return
    start = common_start(datasets)
    base = portfolio(datasets, budget, start=start, **opts)
    dates = base["dates"]
    if len(dates) < 60:
        print(f"  공통 데이터 부족 ({len(dates)}일) — --years 를 늘리세요")
        return
    eq_hold, n_hold = hold_curve(datasets, dates, budget, **opts)
    late = [s for s, b in datasets.items() if not b or b[0]["date"] > dates[0] or dates[0] not in by_date(b)]
    print(f"  기간 {dates[0]}~{dates[-1]} ({len(dates)}일)")
    if late:
        print(f"  ※ 늦게 상장·데이터 시작이라 중간부터 참여(균등 보유 비교에선 제외): {', '.join(late)}")
    print(f"  균등 보유({n_hold}) : {fmt(summary([v / budget for v in eq_hold]))}")
    print(f"     연도별   : {yearly(dates, eq_hold, budget)}")
    for name, slots, rmax in PORTFOLIO_VARIANTS:
        r = portfolio(datasets, budget, slots=slots, rsi_max=rmax, start=start, **opts)
        print_portfolio_row(name, r, budget)
        if r["skipped"]:
            print(f"     칸 예산보다 비싸 한 번도 못 산 종목: {', '.join(sorted(r['skipped']))}")


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
    p.add_argument("--add", help="portfolio 종목 수 비교: 기존 --targets 에 더할 종목, 예: NVDA:NAS,TSLA:NAS")
    p.add_argument("--market", default="SPY:AMS",
                   help="portfolio 시장 필터 기준 종목 (기본 SPY:AMS, none=비교 생략)")
    p.add_argument("--only", choices=["pullback", "portfolio", "breakout", "robust", "trend", "rotation"],
                   help="한 전략만 실행")
    p.add_argument("--fee", type=float, default=None, help="편도 수수료(%%) 덮어쓰기")
    p.add_argument("--csv", nargs="+", help="date,open,high,low,close CSV 파일들로 테스트 (파일명=종목명)")
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    opts = {"fee_pct": a.fee if a.fee is not None else float(g("FEE_PCT", "0.25")), "slip_pct": 0.05}
    safe, market_name, market_bars = None, None, None
    use_market = a.only in (None, "portfolio") and a.market.lower() != "none" and not a.add
    if use_market:
        market_name = a.market.split(":")[0].upper()
    if a.csv:
        datasets = {Path(f).stem: load_csv(f) for f in a.csv}
        if use_market and market_name in datasets:  # CSV 모드: 시장 종목 파일을 따로 뺌
            market_bars = datasets.pop(market_name)
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
                continue
            warn_jumps(sym, datasets[sym])
        extra = {}
        for sym, ex in (parse_targets(a.add) if a.add else {}).items():
            if sym in datasets:
                continue
            print(f"{sym} 일봉 수집 중… (추가 종목)")
            extra[sym] = fetch_bars(api, sym, ex, a.years)
            if not extra[sym]:
                print(f"  ⚠️ {sym}({ex}) 시세 없음 — 거래소 코드를 확인하세요 (제외)")
                extra.pop(sym)
                continue
            warn_jumps(sym, extra[sym])
        if use_market:
            print(f"{market_name} 일봉 수집 중… (시장 필터용)")
            sym, ex = next(iter(parse_targets(a.market).items()))
            market_bars = fetch_bars(api, sym, ex, a.years) or None

    print(f"\n수수료 편도 {opts['fee_pct']}%, 슬리피지 {opts['slip_pct']}%")
    stocks = {s: b for s, b in datasets.items() if s != safe}
    if a.only == "robust":
        run_robust(stocks, {} if a.csv else extra, opts, float(g("BUDGET_USD", "950")))
        print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다.")
        return
    if a.add and not a.csv:
        run_universe(stocks, extra, opts, float(g("BUDGET_USD", "950")))
        print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다.")
        return
    if a.only in (None, "pullback"):
        run_pullback(stocks, opts)
    if a.only in (None, "portfolio"):
        run_portfolio(stocks, opts, float(g("BUDGET_USD", "950")))
        if market_bars:
            run_market_filters(stocks, opts, float(g("BUDGET_USD", "950")), market_name, market_bars)
        elif use_market:
            print(f"\n⚠️ {market_name} 시세를 못 받아 시장 필터 비교 생략")
    if a.only in (None, "breakout"):
        run_breakout(stocks, opts, float(g("BUDGET_USD", "950")))
    if a.only in (None, "trend"):
        run_trend(stocks, opts)
    if a.only in (None, "rotation"):
        run_rotation(datasets, safe, opts)
    print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다. 최대낙폭은 '가장 많이 빠졌을 때'라 함께 보세요.")


if __name__ == "__main__":
    main()
