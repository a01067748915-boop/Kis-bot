"""
기술적 분석 + 기본적 분석 전략 비교 백테스트 (한투 일봉 + SEC 재무제표)

  venv/bin/python analysis.py                                  # 기본: 섹터 균형 39 + 추가 36종목, 10년
  venv/bin/python analysis.py --only tech                      # 기술적 분석만
  venv/bin/python analysis.py --only fund                      # 기본적 분석만
  venv/bin/python analysis.py --targets @universes/tech26.txt --years 5

공통 조건: 예산 BUDGET_USD 하나를 4칸으로 운용, 1주 단위, 신호는 전날 종가, 매매는 그날 시가,
          수수료(FEE_PCT)+슬리피지 0.05%, 늦게 상장한 종목은 데이터가 생긴 뒤부터 참여

기술적 분석 (같은 날 여러 신호면 최근 6개월 수익률 높은 순, 200일선 위 종목만)
  볼린저A  하단 밴드 아래 매수 → 중심선 회복 매도 (평균회귀)
  볼린저B  밴드가 좁아졌다가 상단 돌파 매수 → 중심선 이탈 매도 (스퀴즈)
  MACD일봉 / MACD주봉  MACD가 시그널 위로 새로 올라서고 0 위 → 매수, 시그널 아래 → 매도
  일목균형표  구름 위·전환선>기준선·후행스팬 확인 → 매수, 기준선 아래 → 매도
기본적 분석 (매달 첫 거래일 상위 4종목으로 교체, SEC 에 그날까지 제출된 재무만 사용)
  매출성장 / ROE / 성장+ROE(순위 합) / 성장+ROE+200일선 위만
비교 기준: 균등 보유, 스윙 돌파 40/20, 눌림목 RSI<5, 6개월 모멘텀 상위 4 (월 교체)
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from backtest import compact, try_fetch
from signals import bollinger_indicators, ichimoku_indicators, indicators, macd_indicators, market_indicators
from strategies import (breakout_portfolio, common_start, hold_curve, portfolio, slot_engine,
                        summary, usd, warn_jumps)

HERE = Path(__file__).resolve().parent
SLOTS = 4


# ─── 엔진 연결 ───────────────────────────────────────
def signal_portfolio(datasets, ind_fn, budget, slots=SLOTS, need_trend=True, start=None, ind=None, **opts):
    """지표 함수(날짜별 entry/exit/trend/mom)로 칸 운용. ind 를 주면 미리 계산한 지표 사용"""
    ind = ind or {s: ind_fn(b) for s, b in datasets.items()}

    def wants_exit(s, y, p):
        return ind[s][y]["exit"]

    def candidates(y, d, ready, held):
        return [s for _, s in sorted((-ind[s][y]["mom"], s) for s in ind if s not in held and ready(s, d)
                                     and ind[s][y]["entry"] and (ind[s][y]["trend"] or not need_trend))]

    return slot_engine(datasets, ind, budget, slots, wants_exit, candidates, start=start, **opts)


def factor_portfolio(datasets, score, budget, slots=SLOTS, need_trend=False, start=None, ind=None, raw=None,
                     market_ok=None, every=1, **opts):
    """매달(every개월마다) 첫 거래일 score(종목, 전날) 높은 순 상위 slots 개로 교체. score 가 None 이면 제외
    market_ok(전날) 이 False 면 그 기간은 전부 팔고 현금 (시장 필터). every=12 → 1년에 한 번 교체"""
    ind = ind or {s: indicators(b, 200) for s, b in datasets.items()}
    state = {"month": None, "target": []}

    def update(y):
        period = (int(y[:4]) * 12 + int(y[4:6]) - 1) // every
        if period == state["month"]:
            return
        state["month"] = period
        if market_ok is not None and not market_ok(y):
            state["target"] = []
            return
        ranked = []
        for s in ind:
            if y not in ind[s] or (need_trend and not ind[s][y]["trend"]):
                continue
            v = score(s, y)
            if v is not None:
                ranked.append((-v, s))
        state["target"] = [s for _, s in sorted(ranked)[:slots]]

    def wants_exit(s, y, p):
        update(y)
        return s not in state["target"]

    def candidates(y, d, ready, held):
        update(y)
        return [s for s in state["target"] if s not in held and ready(s, d)]

    return slot_engine(datasets, ind, budget, slots, wants_exit, candidates, start=start, raw=raw, **opts)


def momentum_score(datasets, n=126, skip=0):
    """최근 n거래일 수익률. skip: 최근 skip일은 빼고 계산 (12-1개월 모멘텀 = n 252, skip 21 — 단기 되돌림 회피)"""
    closes = {s: [(b["date"], b["close"]) for b in bs] for s, bs in datasets.items()}
    pos = {s: {d: i for i, (d, _) in enumerate(c)} for s, c in closes.items()}

    def score(s, y):
        i = pos[s].get(y)
        if i is None or i < n:
            return None
        return closes[s][i - skip][1] / closes[s][i - n][1] - 1
    return score


def _ranks(names, y, scores):
    """여러 점수를 그날 종목들 사이 순위(0~1)로 바꿔 합침 — 단위가 다른 지표를 공평하게 섞기 위해.
    점수 하나라도 없는 종목은 제외"""
    total, missing = {s: 0.0 for s in names}, set()
    for f in scores:
        vals = {s: f(s, y) for s in names}
        vals = {s: v for s, v in vals.items() if v is not None}
        missing |= set(names) - set(vals)
        order = sorted(vals, key=vals.get)
        for r, s in enumerate(order):
            total[s] += r / max(1, len(order) - 1)
    return {s: v for s, v in total.items() if s not in missing}


# ─── 결과 출력 ────────────────────────────────────────
def periods(dates):
    end = dates[-1]
    out = [("전체", dates[0])]
    for label, yrs in (("4년", 4), ("2년", 2)):
        cut = f"{int(end[:4]) - yrs}{end[4:]}"
        sub = [d for d in dates if d >= cut]
        if len(sub) > 60:
            out.append((label, sub[0]))
    return out


def line(name, runs, budget):
    """runs: [(구간이름, 결과)] → 한 줄 요약"""
    parts, first = [], runs[0][1]
    for label, r in runs:
        if not r["curve"]:
            continue
        sm = summary([v / budget for v in r["curve"]])
        parts.append(f"{label} 연{sm['연']:+.0f}%/낙폭{sm['낙폭']:.0f}%")
    t = first["trades"]
    n = len(t)
    avg = sum(x[0] for x in t) / n * 100 if n else 0
    days = sum(x[1] for x in t) / n if n else 0
    still = f"(+보유중 {first['open']})" if first.get("open") else ""
    return f"  {name:<12}| " + " | ".join(parts) + f" | 매매 {n}회{still} 1회평균 {avg:+.1f}% 보유 {days:.0f}일"


def hold_line(datasets, starts, dates_all, budget, opts):
    parts = []
    for label, st in starts:
        sub = [d for d in dates_all if d >= st]
        curve, _ = hold_curve(datasets, sub, budget, **opts)
        sm = summary([v / budget for v in curve])
        parts.append(f"{label} 연{sm['연']:+.0f}%/낙폭{sm['낙폭']:.0f}%")
    return f"  {'균등 보유':<11}| " + " | ".join(parts)


def run_all(fn, starts):
    return [(label, fn(st)) for label, st in starts]


TECH = [
    ("볼린저A", lambda b: bollinger_indicators(b, "revert")),
    ("볼린저B", lambda b: bollinger_indicators(b, "squeeze")),
    ("MACD일봉", lambda b: macd_indicators(b)),
    ("MACD주봉", lambda b: macd_indicators(b, weekly=True)),
    ("일목균형표", ichimoku_indicators),
]


def run_tech(datasets, budget, opts, start):
    ref = breakout_portfolio(datasets, budget, slots=SLOTS, entry_n=40, exit_n=20, start=start, **opts)
    starts = periods(ref["dates"])
    print(f"\n━━ 기술적 분석 ({len(datasets)}종목, {SLOTS}칸, {usd(budget)}) — 구간별 '연수익/최대낙폭' ━━")
    print(hold_line(datasets, starts, ref["dates"], budget, opts))
    print(line("스윙돌파40/20", run_all(lambda st: breakout_portfolio(
        datasets, budget, slots=SLOTS, entry_n=40, exit_n=20, start=st, **opts), starts), budget))
    print(line("눌림목RSI<5", run_all(lambda st: portfolio(
        datasets, budget, slots=SLOTS, rsi_max=5, start=st, **opts), starts), budget))
    for name, fn in TECH:
        ind = {s: fn(b) for s, b in datasets.items()}  # 구간마다 다시 계산하지 않게
        print(line(name, run_all(lambda st, ind=ind: signal_portfolio(datasets, None, budget, start=st, ind=ind,
                                                                     **opts), starts), budget))
    return starts, ref["dates"]


def run_fund(datasets, edgar, budget, opts, start, starts=None, dates=None):
    from fundamentals import Fundamentals
    print(f"\n━━ 기본적 분석 (SEC 재무제표, 매달 상위 {SLOTS}종목 교체) ━━")
    funds, missing = {}, []
    for s in datasets:
        try:
            f = edgar.facts(s)
        except RuntimeError as e:
            print(f"  ⚠️ {s} 재무 받기 실패: {e}")
            f = None
        fu = Fundamentals(f) if f else None
        if fu and (fu.rev or fu.ni):
            funds[s] = fu
        else:
            missing.append(s)
    if missing:
        print(f"  ※ 재무제표 없음/부족(제외): {', '.join(missing)}")
    ds = {s: b for s, b in datasets.items() if s in funds}
    if len(ds) < SLOTS + 2:
        print("  재무제표 있는 종목이 너무 적습니다")
        return
    if starts is None:
        ref = factor_portfolio(ds, momentum_score(ds), budget, start=start, **opts)
        dates = ref["dates"]
        starts = periods(dates)
    asof = {}

    def growth(s, y):
        return funds[s].growth(y)

    def roe(s, y):
        return funds[s].roe(y)

    def combo(s, y):
        if y not in asof:
            asof[y] = _ranks(list(ds), y, [growth, roe])
        return asof[y].get(s)

    base = {s: indicators(b, 200) for s, b in ds.items()}
    print(hold_line(ds, starts, dates, budget, opts))
    print(line("모멘텀상위4", run_all(lambda st: factor_portfolio(
        ds, momentum_score(ds), budget, start=st, ind=base, **opts), starts), budget))
    for name, sc, tr in [("매출성장", growth, False), ("ROE", roe, False), ("성장+ROE", combo, False),
                         ("성장+ROE+추세", combo, True)]:
        print(line(name, run_all(lambda st, sc=sc, tr=tr: factor_portfolio(
            ds, sc, budget, need_trend=tr, start=st, ind=base, **opts), starts), budget))


# ─── 검증: 모멘텀 · 매출성장 상위 N ─────────────────
def load_funds(datasets, edgar):
    from fundamentals import Fundamentals
    import requests
    funds = {}
    for i, s in enumerate(datasets, 1):
        if i % 25 == 0:
            print(f"  … SEC {i}/{len(datasets)}종목")
        try:
            f = edgar.facts(s)
        except (RuntimeError, ValueError, requests.RequestException) as e:  # 한 종목 실패로 전체가 멈추지 않게
            print(f"  ⚠️ {s} 재무 받기 실패: {e}")
            continue
        fu = Fundamentals(f) if f else None
        if fu and fu.rev:
            funds[s] = fu
    return funds


GROWTH_MIN_PREV = 10e6  # 매출성장 기본 규칙: 1년 전 4분기 매출 1,000만 달러 이상


def growth_score(funds, min_prev=GROWTH_MIN_PREV, cap=None):
    def score(s, y):
        return funds[s].growth(y, min_prev, cap) if s in funds else None
    return score


GROWTH_VARIANTS = [  # (이름, min_prev, cap)
    ("매출성장 조건없음", None, None), ("매출성장 1년전≥1천만$", 10e6, None),
    ("+상한300%", 10e6, 3.0), ("매출성장 1년전≥5천만$", 50e6, None),
]


def run_growthfix(groups, raw, funds, budget, opts, picks=()):
    """매출성장 규칙의 '작은 기반에서 튀는 성장률' 처리 방법 비교"""
    full = groups[-1][1]
    start = common_start(full)
    print(f"\n━━ 매출성장 규칙 기준 비교 — 매달 상위 {SLOTS}종목, {usd(budget)}, 실제 가격으로 1주 단위 ━━")
    print("  기준: 1년 전 4분기 매출이 기준 미만이면 순위에서 제외 / 상한: 성장률을 +300%로 잘라 순위 계산")
    for gname, ds in groups:
        ref = factor_portfolio(ds, momentum_score(ds), budget, start=start, raw=raw, **opts)
        if not ref["dates"]:
            continue
        starts = periods(ref["dates"])
        base = {s: indicators(b, 200) for s, b in ds.items()}
        print(f"\n  ■ {gname} ({len(ds)}종목)")
        print("  " + hold_line(ds, starts, ref["dates"], budget, opts))
        print("  " + line("모멘텀", run_all(lambda st: factor_portfolio(
            ds, momentum_score(ds), budget, start=st, ind=base, raw=raw, **opts), starts), budget))
        for name, mp, cap in GROWTH_VARIANTS:
            print("  " + line(name, run_all(lambda st, mp=mp, cap=cap: factor_portfolio(
                ds, growth_score(funds, mp, cap), budget, start=st, ind=base, raw=raw, **opts), starts), budget))
    if picks:
        last = max(b[-1]["date"] for b in full.values())
        print(f"\n  ■ 관심 종목 순위 ({last} 기준, 상위 {SLOTS}위 안이면 그달에 매수)")
        for name, mp, cap in GROWTH_VARIANTS:
            sc = {s: funds[s].growth(last, mp, cap) for s in full if s in funds}
            ranked = [s for s, v in sorted(((s, v) for s, v in sc.items() if v is not None), key=lambda x: -x[1])]
            marks = []
            for s in picks:
                g = funds[s].growth(last) if s in funds else None
                pos = f"{ranked.index(s) + 1}위/{len(ranked)}" if s in ranked else "순위 밖"
                marks.append(f"{s} {pos}" + (f" (성장 {g * 100:+,.0f}%)" if g is not None else ""))
            print(f"    {name:<16}: " + ", ".join(marks) + f" | 1~{SLOTS}위: {', '.join(ranked[:SLOTS])}")


def combo_score(names, *scores):
    cache = {}

    def score(s, y):
        if y not in cache:
            cache[y] = _ranks(names, y, scores)
        return cache[y].get(s)
    return score


MIN_REV = 50e6  # GARP 규칙: 최근 4분기 매출 5,000만 달러 미만(상용화 전 등)은 성장률이 튀어 제외


def garp_scores(ds, funds, raw):
    """A 덜 오른 고성장주 / B 성장 대비 싼 주식(성장률÷PSR) / C 고점 대비 빠진 고성장주 — 날짜별 캐시"""
    closes = {s: [b["close"] for b in bs] for s, bs in ds.items()}
    pos = {s: {b["date"]: i for i, b in enumerate(bs)} for s, bs in ds.items()}
    rawpx = {s: {b["date"]: b for b in bs} for s, bs in raw.items()}
    cache = {}

    def table(y):
        if y in cache:
            return cache[y]
        rows = {}
        for s in ds:
            f, i = funds.get(s), pos[s].get(y)
            if not f or i is None or i < 252:
                continue
            rev, g = f.revenue(y), f.growth(y)
            if rev is None or rev < MIN_REV or g is None:
                continue
            c = closes[s]
            rp = rawpx.get(s, {}).get(y)
            rows[s] = {"g": g, "ret6": c[i] / c[i - 126] - 1, "dd": 1 - c[i] / max(c[i - 251:i + 1]),
                       "psr": f.psr(y, rp["close"]) if rp else None}
        if rows:
            gs = sorted(r["g"] for r in rows.values())
            med = gs[len(gs) // 2]
            for r in rows.values():
                r["top"] = r["g"] >= med
        cache[y] = rows
        return rows

    def a(s, y):
        r = table(y).get(s)
        return -r["ret6"] if r and r["top"] else None

    def b(s, y):
        r = table(y).get(s)
        return r["g"] / r["psr"] if r and r["g"] > 0 and r["psr"] and r["psr"] > 0 else None

    def c(s, y):
        r = table(y).get(s)
        return r["dd"] if r and r["top"] else None
    return a, b, c


def run_garp(groups, raw, funds, budget, opts, picks=()):
    full = groups[-1][1]
    start = common_start(full)
    print(f"\n━━ 덜 오른 성장주(GARP) 규칙 비교 — 매달 상위 {SLOTS}종목, {usd(budget)}, 실제 가격으로 1주 단위 ━━")
    print("  A 덜오른고성장: 매출성장 상위 절반 중 6개월 상승률 가장 낮은 순")
    print("  B 성장÷PSR    : 매출성장률 ÷ 주가매출비율 높은 순 (성장 대비 싼 주식)")
    print("  C 고점대비하락: 매출성장 상위 절반 중 52주 고점 대비 많이 빠진 순")
    print(f"  ※ A·B·C 는 최근 4분기 매출 {MIN_REV / 1e6:.0f}백만 달러 이상만 (매출이 거의 없으면 성장률이 튐)")
    picked = {}
    for gname, ds in groups:
        ref = factor_portfolio(ds, momentum_score(ds), budget, start=start, raw=raw, **opts)
        if not ref["dates"]:
            continue
        starts = periods(ref["dates"])
        base = {s: indicators(b, 200) for s, b in ds.items()}
        a, b, c = garp_scores(ds, funds, raw)
        print(f"\n  ■ {gname} ({len(ds)}종목)")
        print("  " + hold_line(ds, starts, ref["dates"], budget, opts))
        for name, sc in [("매출성장", growth_score(funds)), ("모멘텀", momentum_score(ds)),
                         ("A 덜오른고성장", a), ("B 성장÷PSR", b), ("C 고점대비하락", c)]:
            runs = run_all(lambda st, sc=sc: factor_portfolio(ds, sc, budget, start=st, ind=base, raw=raw, **opts),
                           starts)
            print("  " + line(name, runs, budget))
            if ds is full:
                r = runs[0][1]
                picked[name] = {t[2] for t in r["trades"]} | set(r.get("open_syms", []))
    if picks:
        print("\n  ■ 관심 종목 점검 (가장 최근 기준)")
        for s in picks:
            if s not in full:
                print(f"    {s}: 시세 없음 — 거래소 코드 확인")
                continue
            last = full[s][-1]["date"]
            f = funds.get(s)
            rev = f.revenue(last) if f else None
            g = f.growth(last) if f else None
            rp = raw.get(s, [{}])[-1].get("close") if raw.get(s) else None
            psr = f.psr(last, rp) if f and rp else None
            chosen = [n for n, syms in picked.items() if s in syms]
            info = (f"데이터 {full[s][0]['date']}~, 최근 4분기 매출 "
                    + (f"{rev / 1e6:,.1f}백만 달러" if rev is not None else "없음")
                    + (f", 매출성장 {g * 100:+.0f}%" if g is not None else "")
                    + (f", PSR {psr:,.0f}배" if psr else ""))
            why = "" if (rev or 0) >= MIN_REV else f" → 매출 {MIN_REV / 1e6:.0f}백만 달러 미만이라 A·B·C 에서 제외"
            print(f"    {s}: {info}{why}")
            print(f"      과거에 고른 규칙: {', '.join(chosen) if chosen else '없음'}")


# ─── 견고성: 큰 승자 빼기 · 무작위 묶음 ─────────────────
def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def contributors(r, k=5):
    """전략이 실제로 번 종목 순위 (끝난 매매 수익률 합)"""
    tot = {}
    for ret, _, sym in r["trades"]:
        tot[sym] = tot.get(sym, 0.0) + ret
    return [s for s, v in sorted(tot.items(), key=lambda x: -x[1])[:k]]


def run_stress(full, raw, funds, budget, opts, trials=200, size=40, seed=1):
    import random
    start = common_start(full)
    base_ind = {s: indicators(b, 200) for s, b in full.items()}  # 한 번만 계산해 재사용

    def strat(ds, name):
        sc = momentum_score(ds) if name == "모멘텀" else growth_score(funds)
        sub = {s: base_ind[s] for s in ds}
        return lambda st: factor_portfolio(ds, sc, budget, start=st, ind=sub, raw=raw, **opts)

    names = ["모멘텀", "매출성장"]
    ref = strat(full, "모멘텀")(start)
    dates = ref["dates"]
    if not dates:
        print("  데이터 부족")
        return
    starts = periods(dates)
    print(f"\n━━ 견고성 검증 ({len(full)}종목, 매달 상위 {SLOTS}, {usd(budget)}, 실제 가격, 매출성장은 1년전≥1천만$) ━━")

    # ① 큰 승자 빼기
    first = {s: next((b for b in bs if b["date"] >= dates[0]), None) for s, bs in full.items()}
    hold_ret = {s: bs[-1]["close"] / first[s]["open"] - 1 for s, bs in full.items() if first[s]}
    winners = [s for s, _ in sorted(hold_ret.items(), key=lambda x: -x[1])]
    print("\n① 큰 승자 빼기")
    print("  보유 수익 상위 10: " + ", ".join(f"{s}({hold_ret[s] * 100:+,.0f}%)" for s in winners[:10]))
    for label, drop in (("전체", []), ("상위 5 제외", winners[:5]), ("상위 10 제외", winners[:10])):
        ds = {s: b for s, b in full.items() if s not in drop}
        print(f"  ■ {label} ({len(ds)}종목)")
        print("  " + hold_line(ds, starts, dates, budget, opts))
        for n in names:
            print("  " + line(n, run_all(strat(ds, n), starts), budget))
    for n in names:
        top = contributors(strat(full, n)(start))
        if not top:
            print(f"  ■ {n}: 끝난 매매가 없어 기여 종목 제외 검사 생략")
            continue
        ds = {s: b for s, b in full.items() if s not in top}
        print(f"  ■ {n}이 가장 많이 번 5종목 제외: {', '.join(top)}")
        print("  " + hold_line(ds, starts, dates, budget, opts))
        print("  " + line(n, run_all(strat(ds, n), starts), budget))

    # ② 무작위 묶음
    rng = random.Random(seed)
    pool = sorted(full)
    size = min(size, len(pool))
    print(f"\n② 무작위 {size}종목 × {trials}회 (전체 기간, 뽑는 순서 고정 seed={seed})")
    res = {n: {"ex": [], "mdd": []} for n in names}
    hold_mdd = []
    for t in range(trials):
        pick = rng.sample(pool, size)
        ds = {s: full[s] for s in pick}
        hc, _ = hold_curve(ds, dates, budget, **opts)
        hs = summary([v / budget for v in hc])
        hold_mdd.append(hs["낙폭"])
        for n in names:
            r = strat(ds, n)(start)
            sm = summary([v / budget for v in r["curve"]])
            res[n]["ex"].append(sm["연"] - hs["연"])
            res[n]["mdd"].append(sm["낙폭"])
        if (t + 1) % 50 == 0:
            print(f"    … {t + 1}/{trials}회")
    print(f"  균등 보유 최대낙폭 중앙값 {_pct(hold_mdd, 0.5):.0f}%")
    for n in names:
        ex = res[n]["ex"]
        win = sum(x > 0 for x in ex) / len(ex) * 100
        print(f"  {n:<6}: 보유보다 높은 경우 {win:.0f}% | 연수익 차이 중앙값 {_pct(ex, 0.5):+.0f}%p"
              f" (하위10% {_pct(ex, 0.1):+.0f}%p ~ 상위10% {_pct(ex, 0.9):+.0f}%p)"
              f" | 최대낙폭 중앙값 {_pct(res[n]['mdd'], 0.5):.0f}%")
    print("  → '보유보다 높은 경우'가 80% 이상이고 하위10%도 0 근처면, 특정 종목 구성에 기대지 않는 전략")


def quality_scores(funds, raw):
    """장기 보유용 점수 함수들 — 수익성(ROE·영업이익률), 매출성장, 저평가(주가매출비율 낮을수록 높게)"""
    rawpx = {s: {b["date"]: b["close"] for b in bs} for s, bs in raw.items()}

    def roe(s, y):
        f = funds.get(s)
        return f.roe(y) if f and (f.revenue(y) or 0) >= MIN_REV else None

    def opm(s, y):
        f = funds.get(s)
        return f.opm(y) if f and (f.revenue(y) or 0) >= MIN_REV else None

    def growth(s, y):
        return funds[s].growth(y, GROWTH_MIN_PREV) if s in funds else None

    def cheap(s, y):
        f, px = funds.get(s), rawpx.get(s, {}).get(y)
        v = f.psr(y, px) if f and px else None
        return -v if v else None
    return {"roe": roe, "opm": opm, "growth": growth, "cheap": cheap}


LONG_VARIANTS = [  # (이름, 점수 조합, 칸 수, 교체 주기(개월))
    ("수익성 연1회·10", ("roe", "opm"), 10, 12),
    ("수익성+성장 연1회·10", ("roe", "opm", "growth"), 10, 12),
    ("수익성+저평가 연1회·10", ("roe", "opm", "cheap"), 10, 12),
    ("수익성 연1회·20", ("roe", "opm"), 20, 12),
    ("수익성 분기·10", ("roe", "opm"), 10, 3),
]


def run_longhold(full, raw, funds, budget, opts, trials=100, size=60, seed=1, big=5000.0):
    """재무 기준으로 골라 길게 보유 — 연 1회(또는 분기) 교체, 10·20종목. 무작위 묶음·큰 승자 빼기로 견고성 확인"""
    import random
    start = common_start(full)
    base_ind = {s: indicators(b, 200) for s, b in full.items()}
    fs = quality_scores(funds, raw)

    def strat(ds, v, bud):
        _, keys, slots, every = v
        sc = combo_score(list(ds), *[fs[k] for k in keys])
        sub = {s: base_ind[s] for s in ds}
        return lambda st: factor_portfolio(ds, sc, bud, slots=slots, start=st, ind=sub, raw=raw, every=every, **opts)

    ref = strat(full, LONG_VARIANTS[0], big)(start)
    dates = ref["dates"]
    if not dates:
        print("  데이터 부족")
        return
    starts = periods(dates)
    print(f"\n━━ 장기 보유형 재무 전략 ({len(full)}종목 중 재무 있는 {len(funds)}종목, 실제 가격, "
          f"매출 {MIN_REV / 1e6:.0f}백만$ 이상만) ━━")
    for bud in (budget, big):
        print(f"\n① 전체 종목 — 예산 {usd(bud)}")
        print("  " + hold_line(full, starts, dates, bud, opts))
        for v in LONG_VARIANTS:
            r = run_all(strat(full, v, bud), starts)
            print("  " + line(v[0], r, bud) + f" | 1주 못 사 건너뜀 {r[0][1]['skips']}회")
    print(f"\n② 각 전략이 가장 많이 번 5종목 빼고 다시 ({usd(big)})")
    for v in LONG_VARIANTS:
        top = contributors(strat(full, v, big)(start))
        if not top:
            continue
        ds = {s: b for s, b in full.items() if s not in top}
        print(f"  ■ {v[0]} 기여 상위 제외: {', '.join(top)}")
        print("  " + hold_line(ds, starts, dates, big, opts))
        print("  " + line(v[0], run_all(strat(ds, v, big), starts), big))

    rng = random.Random(seed)
    pool = sorted(full)
    size = min(size, len(pool))
    print(f"\n③ 무작위 {size}종목 × {trials}회 ({usd(big)}, 전체 기간, seed={seed})")
    res = {v[0]: {"ex": [], "mdd": []} for v in LONG_VARIANTS}
    hold_mdd = []
    for t in range(trials):
        ds = {s: full[s] for s in rng.sample(pool, size)}
        hc, _ = hold_curve(ds, dates, big, **opts)
        hs = summary([x / big for x in hc])
        hold_mdd.append(hs["낙폭"])
        for v in LONG_VARIANTS:
            sm = summary([x / big for x in strat(ds, v, big)(start)["curve"]])
            res[v[0]]["ex"].append(sm["연"] - hs["연"])
            res[v[0]]["mdd"].append(sm["낙폭"])
        if (t + 1) % 25 == 0:
            print(f"    … {t + 1}/{trials}회")
    print(f"  균등 보유 최대낙폭 중앙값 {_pct(hold_mdd, 0.5):.0f}%")
    for v in LONG_VARIANTS:
        ex = res[v[0]]["ex"]
        win = sum(x > 0 for x in ex) / len(ex) * 100
        print(f"  {v[0]:<18}: 보유보다 높은 경우 {win:3.0f}% | 연수익 차이 중앙값 {_pct(ex, 0.5):+.0f}%p"
              f" (하위10% {_pct(ex, 0.1):+.0f}%p ~ 상위10% {_pct(ex, 0.9):+.0f}%p)"
              f" | 최대낙폭 중앙값 {_pct(res[v[0]]['mdd'], 0.5):.0f}%")
    print("  → '보유보다 높은 경우' 80% 이상 + 하위10% 0 근처면 견고. 반반이면 지수 보유가 나음")


MOM_VARIANTS = [  # (이름, 칸 수, 기간, 최근 제외일, 시장 필터)
    ("6개월·상위4(기존)", 4, 126, 0, False),
    ("12-1개월·상위4", 4, 252, 21, False),
    ("6개월·상위10", 10, 126, 0, False),
    ("6개월·상위20", 20, 126, 0, False),
    ("6개월·상위4+시장", 4, 126, 0, True),
    ("12-1·상위10+시장", 10, 252, 21, True),
]


def run_momvar(full, raw, budget, opts, market, trials=200, size=40, seed=1, big=5000.0):
    """모멘텀 변형 비교 — 넓게 분산(10·20칸) / 12-1개월 / 시장 필터(SPY 200일선 아래면 현금)
    market: {날짜: SPY 가 200일선 위인지}"""
    import random
    start = common_start(full)
    base_ind = {s: indicators(b, 200) for s, b in full.items()}
    mok = (lambda y: market.get(y, True)) if market else None

    def strat(ds, v, bud=budget):
        _, slots, n, skip, filt = v
        sc = momentum_score(ds, n, skip)
        sub = {s: base_ind[s] for s in ds}
        return lambda st: factor_portfolio(ds, sc, bud, slots=slots, start=st, ind=sub, raw=raw,
                                           market_ok=mok if filt else None, **opts)

    variants = [v for v in MOM_VARIANTS if market or not v[4]]
    ref = strat(full, variants[0])(start)
    dates = ref["dates"]
    if not dates:
        print("  데이터 부족")
        return
    starts = periods(dates)
    print(f"\n━━ 모멘텀 변형 비교 ({len(full)}종목, 매달 교체, {usd(budget)}, 실제 가격) ━━")
    if not market:
        print("  ⚠️ SPY 시세가 없어 시장 필터 변형은 생략")
    print("\n① 전체 종목")
    print("  " + hold_line(full, starts, dates, budget, opts))
    for v in variants:
        r = run_all(strat(full, v), starts)
        print("  " + line(v[0], r, budget) + f" | 1주 못 사 건너뜀 {r[0][1]['skips']}회")
    v20 = next(v for v in variants if v[1] == 20)
    print(f"  (참고) {v20[0]}을 {usd(big)}로: "
          + line("", run_all(strat(full, v20, big), starts), big).split("| ", 1)[1])
    print("  → 칸이 많으면 칸당 금액이 작아 비싼 종목은 1주도 못 삼 (건너뜀 횟수 참고)")

    rng = random.Random(seed)
    pool = sorted(full)
    size = min(size, len(pool))
    print(f"\n② 무작위 {size}종목 × {trials}회 (전체 기간, seed={seed})")
    res = {v[0]: {"ex": [], "mdd": []} for v in variants}
    hold_mdd = []
    for t in range(trials):
        ds = {s: full[s] for s in rng.sample(pool, size)}
        hc, _ = hold_curve(ds, dates, budget, **opts)
        hs = summary([x / budget for x in hc])
        hold_mdd.append(hs["낙폭"])
        for v in variants:
            sm = summary([x / budget for x in strat(ds, v)(start)["curve"]])
            res[v[0]]["ex"].append(sm["연"] - hs["연"])
            res[v[0]]["mdd"].append(sm["낙폭"])
        if (t + 1) % 25 == 0:
            print(f"    … {t + 1}/{trials}회")
    print(f"  균등 보유 최대낙폭 중앙값 {_pct(hold_mdd, 0.5):.0f}%")
    for v in variants:
        ex = res[v[0]]["ex"]
        win = sum(x > 0 for x in ex) / len(ex) * 100
        print(f"  {v[0]:<14}: 보유보다 높은 경우 {win:3.0f}% | 연수익 차이 중앙값 {_pct(ex, 0.5):+.0f}%p"
              f" (하위10% {_pct(ex, 0.1):+.0f}%p ~ 상위10% {_pct(ex, 0.9):+.0f}%p)"
              f" | 최대낙폭 중앙값 {_pct(res[v[0]]['mdd'], 0.5):.0f}%")
    print("  → '보유보다 높은 경우' 80% 이상 + 하위10% 0 근처면 견고. 낙폭이 보유보다 작으면 위험 대비 개선")


def cell(r, budget):
    sm = summary([v / budget for v in r["curve"]])
    return f"연{sm['연']:+.0f}/{sm['낙폭']:.0f}"


def run_verify(groups, raw, funds, budget, opts):
    """groups: [(이름, {종목: 일봉})] — 마지막이 전체. raw: 실제 가격 일봉"""
    full = groups[-1][1]
    start = common_start(full)
    print(f"\n━━ 검증: 모멘텀 · 매출성장 상위 N (매달 교체, {usd(budget)}, 실제 가격으로 1주 단위) ━━")
    print("  표기: 구간별 '연수익/최대낙폭' (%). 실제가격 = 그날 실제 주가로 살 수 있는 만큼만 매수")

    def strategies_for(ds, n=SLOTS, mom_n=126):
        names = list(ds)
        g = growth_score(funds)
        m = momentum_score(ds, mom_n)
        return [("모멘텀", m), ("매출성장", g), ("모멘텀+매출성장", combo_score(names, m, g))]

    # ① 종목 묶음
    print("\n① 종목 묶음별 (상위 4, 실제가격)")
    for gname, ds in groups:
        ref = factor_portfolio(ds, momentum_score(ds), budget, start=start, raw=raw, **opts)
        if not ref["dates"]:
            print(f"  {gname}: 데이터 부족")
            continue
        starts = periods(ref["dates"])
        print(f"  ■ {gname} ({len(ds)}종목)")
        print("  " + hold_line(ds, starts, ref["dates"], budget, opts))
        base = {s: indicators(b, 200) for s, b in ds.items()}
        for name, sc in strategies_for(ds):
            print("  " + line(name, run_all(lambda st, sc=sc: factor_portfolio(
                ds, sc, budget, start=st, ind=base, raw=raw, **opts), starts), budget))

    # ② 설정값
    base = {s: indicators(b, 200) for s, b in full.items()}
    print(f"\n② 설정값 ({len(full)}종목, 실제가격, 전체 기간) — '연수익/최대낙폭'")
    print("  모멘텀 기간 \\ 상위   3개         4개         6개")
    for label, n in (("3개월", 63), ("6개월", 126), ("12개월", 252)):
        cells = [cell(factor_portfolio(full, momentum_score(full, n), budget, slots=k, start=start, ind=base,
                                       raw=raw, **opts), budget) for k in (3, 4, 6)]
        print(f"  {label:<8}            " + "   ".join(f"{c:<10}" for c in cells))
    cells = [cell(factor_portfolio(full, growth_score(funds), budget, slots=k, start=start, ind=base, raw=raw,
                                   **opts), budget) for k in (3, 4, 6)]
    print(f"  {'매출성장':<8}           " + "   ".join(f"{c:<10}" for c in cells))
    print("  → 칸마다 비슷하게 좋으면 설정을 운 좋게 고른 게 아님. 6개는 칸당 금액이 작아 1주 못 사는 경우가 늘어남")

    # ③ 실제 가격 반영 효과
    print(f"\n③ 실제 가격 반영 효과 ({len(full)}종목, 상위 4, 전체 기간)")
    for name, sc in strategies_for(full):
        a = factor_portfolio(full, sc, budget, start=start, ind=base, **opts)
        b = factor_portfolio(full, sc, budget, start=start, ind=base, raw=raw, **opts)
        print(f"  {name:<14}: 수정주가 기준 {cell(a, budget)} → 실제가격 기준 {cell(b, budget)}"
              f" (1주 못 사 건너뛴 횟수 {b['skips']}, 끝내 못 산 종목 {len(b['skipped'])}개)")
    print("  → 차이가 크면 백테스트가 소액 계좌에서 실제로는 불가능한 매수를 가정했던 것")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--targets", default="@universes/growth_balanced.txt")
    p.add_argument("--add", default="@universes/growth_extra.txt", help="함께 쓸 종목 (none=안 씀)")
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--only", choices=["tech", "fund", "verify", "garp", "growthfix", "stress", "momvar", "longhold"])
    p.add_argument("--trials", type=int, default=200, help="stress: 무작위 묶음 반복 횟수")
    p.add_argument("--size", type=int, default=40, help="stress: 무작위 묶음 종목 수")
    p.add_argument("--picks", default="@universes/my_picks.txt", help="관심 종목 (garp 에서 따로 점검, none=안 씀)")
    p.add_argument("--fee", type=float, default=None)
    p.add_argument("--cache-days", type=int, default=30, help="받은 시세를 며칠간 재사용 (백테스트는 길게 둬도 됨)")
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    from kis_api import KIS, parse_targets
    targets = parse_targets(a.targets)
    if a.add and a.add.lower() != "none":
        targets.update(parse_targets(a.add))
    picks = {}
    if a.only in ("garp", "growthfix") and a.picks and a.picks.lower() != "none":
        picks = parse_targets(a.picks)
        targets.update(picks)
    api = KIS(g("KIS_ENV", "mock"), g("KIS_APP_KEY"), g("KIS_APP_SECRET"), g("KIS_ACCOUNT"), HERE)
    datasets = {}
    for sym, ex in targets.items():
        print(f"{sym} 일봉 수집 중…")
        bars = try_fetch(api, sym, ex, a.years, max_age_days=a.cache_days)
        if not bars:
            print(f"  ⚠️ {sym}({ex}) 시세 없음 — 거래소 코드를 확인하세요 (제외)")
            continue
        warn_jumps(sym, bars)
        datasets[sym] = compact(bars)
    budget = float(g("BUDGET_USD", "950"))
    opts = {"fee_pct": a.fee if a.fee is not None else float(g("FEE_PCT", "0.25")), "slip_pct": 0.05}
    if a.only in ("verify", "garp", "growthfix", "stress", "momvar", "longhold"):
        raw = {}
        for sym, ex in targets.items():
            if sym in datasets:
                print(f"{sym} 실제 가격(분할 미반영) 수집 중…")
                raw[sym] = compact(try_fetch(api, sym, ex, a.years, adjusted=False, max_age_days=a.cache_days),
                                   keys=("open", "close"))
        if a.only == "momvar":
            print("SPY(시장 필터) 일봉 수집 중…")
            spy = try_fetch(api, "SPY", "AMS", a.years, max_age_days=a.cache_days)
            market = {d: v["trend"] for d, v in market_indicators(spy).items()} if spy else {}
            run_momvar(datasets, raw, budget, opts, market, a.trials, a.size)
            print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다.")
            return
        from fundamentals import Edgar
        edgar = Edgar(g("SEC_USER_AGENT"))
        print("SEC 재무제표 확인 중…")
        funds = load_funds(datasets, edgar)
        base_syms = set(parse_targets(a.targets))
        add_syms = set(targets) - base_syms
        groups = [(f"기본 목록({a.targets})", {s: b for s, b in datasets.items() if s in base_syms})]
        if add_syms:
            groups.append((f"추가 목록({a.add}{' + 관심 종목' if picks else ''})",
                           {s: b for s, b in datasets.items() if s in add_syms}))
            groups.append(("합계", datasets))
        if a.only == "garp":
            run_garp(groups, raw, funds, budget, opts, list(picks))
        elif a.only == "growthfix":
            run_growthfix(groups, raw, funds, budget, opts, list(picks))
        elif a.only == "stress":
            run_stress(datasets, raw, funds, budget, opts, a.trials, a.size)
        elif a.only == "longhold":
            run_longhold(datasets, raw, funds, budget, opts, a.trials, a.size)
        else:
            run_verify(groups, raw, funds, budget, opts)
        print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다.")
        return
    start = common_start(datasets)
    print(f"\n{len(datasets)}종목, 시작 {start}, 수수료 편도 {opts['fee_pct']}%")
    starts = dates = None
    if a.only != "fund":
        starts, dates = run_tech(datasets, budget, opts, start)
    if a.only != "tech":
        from fundamentals import Edgar
        try:
            edgar = Edgar(g("SEC_USER_AGENT"))
        except ValueError as e:
            print(f"\n⚠️ 기본적 분석 생략: {e}")
        else:
            run_fund(datasets, edgar, budget, opts, start, starts, dates)
    print("\n※ 각 구간은 그 시점에 새로 시작했을 때의 결과. 과거 성과가 미래 수익을 보장하지 않습니다.")


if __name__ == "__main__":
    main()
