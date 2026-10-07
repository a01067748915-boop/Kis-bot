"""
Gate.io 봇 백테스트 — bot.py 와 '같은 신호 함수'로 과거 봉을 따라가며 수수료·슬리피지 차감 후 손익 계산

  python3 gate_backtest.py              # .env 설정(종목·봉·EMA·손절/익절)으로 현물·선물 각각
  python3 gate_backtest.py --grid       # 봉 간격(15m/1h/4h) × EMA 조합 비교까지
  python3 gate_backtest.py --slip 0.1   # 슬리피지(편도 %) 바꾸기

가정
  - 마감된 봉에서 신호 → 다음 봉 시가에 진입/청산 (봇과 같은 타이밍)
  - 손절·익절은 봉의 고가·저가로 판단, 한 봉에서 둘 다 닿으면 손절로 가정(보수적). 갭이면 시가에 체결
  - 수수료: .env 의 SPOT_FEE_PCT / FUT_FEE_PCT (편도 %), 슬리피지 기본 편도 0.05%
  - 선물: 증거금 ORDER_USDT × 레버리지 만큼 진입, 펀딩비·강제청산은 미반영 (손절이 강제청산보다 먼저라고 가정)
  - 하루 손실 한도(DAILY_LOSS_LIMIT_USDT)도 봇처럼 적용
  - Gate.io 는 짧은 봉일수록 과거 데이터를 적게 줌(약 1만 개) → 15분봉은 약 3개월, 4시간봉은 약 4년
받은 봉은 data/ 에 그날 하루 저장해 재사용 (API 키 불필요)
"""
import argparse
import json
import os
import time
from datetime import datetime

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


# ─── 데이터 ───────────────────────────────────────────
def fetch(gate, market, sym, interval, max_points=10000, cache_dir=os.path.join(HERE, "data")):
    """과거 봉 [{t,o,h,l,c}] 오래된 순 — 1000개씩 과거로, 거래소가 더 안 주면 멈춤"""
    path = os.path.join(cache_dir, f"{market}_{sym}_{interval}.json")
    today = time.strftime("%Y%m%d")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("day") == today:
            return saved["bars"]
    sec = SEC[interval]
    now = int(time.time())
    to = now // sec * sec - sec  # 마지막 '마감된' 봉
    bars = {}
    while len(bars) < max_points:
        frm = to - sec * 999
        try:
            if market == "spot":
                rows = gate.req("GET", "/spot/candlesticks",
                                {"currency_pair": sym, "interval": interval, "from": frm, "to": to})
                got = [{"t": int(r[0]), "o": float(r[5]), "h": float(r[3]), "l": float(r[4]), "c": float(r[2])}
                       for r in rows]
            else:
                rows = gate.req("GET", "/futures/usdt/candlesticks",
                                {"contract": sym, "interval": interval, "from": frm, "to": to})
                got = [{"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                        "c": float(r["c"])} for r in rows]
        except RuntimeError as e:
            if not bars:
                raise
            print(f"  ℹ️ {sym} {interval}: 더 과거 데이터는 거래소가 제공하지 않음 ({str(e)[:80]})")
            break
        if not got:
            break
        for b in got:
            if b["t"] + sec <= now:
                bars[b["t"]] = b
        to = frm - sec
        time.sleep(0.25)
    out = [bars[t] for t in sorted(bars)]
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": today, "bars": out}, f)
    return out


# ─── 시뮬레이션 ───────────────────────────────────────
def simulate(bars, p, market, fee_pct, slip_pct=0.05):
    """p: 설정(dict) — ema_fast, ema_slow, rsi_period, rsi_max_long, rsi_min_short, stop_loss_pct,
    take_profit_pct, order_usdt, leverage, allow_short, reverse_on_signal, daily_loss_limit_usdt
    → {"trades": [(진입t, 청산t, 방향, 순손익, 총손익(수수료 전), 사유)], "curve": [(t, 누적손익)], "fees"}"""
    fee, slip = fee_pct / 100, slip_pct / 100
    closes = [b["c"] for b in bars]
    if len(bars) < p["ema_slow"] + 5:
        return {"trades": [], "curve": [], "fees": 0.0}
    f, s = bot.ema(closes, p["ema_fast"]), bot.ema(closes, p["ema_slow"])
    r = bot.rsi(closes, p["rsi_period"])
    lev = p["leverage"] if market == "fut" else 1
    notional = p["order_usdt"] * lev
    sl, tp = p["stop_loss_pct"] / 100, p["take_profit_pct"] / 100
    pos, pending, trades, curve, total, fees = None, None, [], [], 0.0, 0.0
    day, day_pnl = "", 0.0

    def day_of(t):
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d")

    def open_pos(side, t, px):
        nonlocal fees
        entry = px * (1 + slip) if side == "long" else px * (1 - slip)
        fees += notional * fee
        return {"side": side, "entry": entry, "t": t}

    def close_pos(t, px, why):
        nonlocal pos, total, fees, day, day_pnl
        d = 1 if pos["side"] == "long" else -1
        exit_px = px * (1 - slip) if d == 1 else px * (1 + slip)
        gross = notional * d * (exit_px / pos["entry"] - 1)
        cost = notional * fee + notional * exit_px / pos["entry"] * fee
        fees += notional * exit_px / pos["entry"] * fee
        net = gross - cost
        trades.append((pos["t"], t, pos["side"], net, gross, why))
        total += net
        if day_of(t) != day:
            day, day_pnl = day_of(t), 0.0
        day_pnl += net
        pos = None

    def halted(t):
        return day == day_of(t) and day_pnl <= -p["daily_loss_limit_usdt"]

    for i in range(p["ema_slow"] + 5, len(bars)):
        b = bars[i]
        # 1) 전 봉 신호를 이번 봉 시가에 실행
        if pending:
            sig = pending
            pending = None
            if market == "spot":
                if sig == "long" and not pos and not halted(b["t"]):
                    pos = open_pos("long", b["t"], b["o"])
                elif sig == "short" and pos:
                    close_pos(b["t"], b["o"], "반대 신호")
            else:
                reopen = True
                if pos and pos["side"] != sig:
                    close_pos(b["t"], b["o"], "반대 신호")
                    reopen = p["reverse_on_signal"]
                if reopen and not pos and not halted(b["t"]) and (sig == "long" or p["allow_short"]):
                    pos = open_pos(sig, b["t"], b["o"])
        # 2) 봉 안에서 손절·익절 (둘 다 닿으면 손절 가정)
        if pos:
            e = pos["entry"]
            if pos["side"] == "long":
                stop, take = e * (1 - sl), e * (1 + tp)
                if b["l"] <= stop:
                    close_pos(b["t"], min(b["o"], stop), "손절")
                elif b["h"] >= take:
                    close_pos(b["t"], max(b["o"], take), "익절")
            else:
                stop, take = e * (1 + sl), e * (1 - tp)
                if b["h"] >= stop:
                    close_pos(b["t"], max(b["o"], stop), "손절")
                elif b["l"] <= take:
                    close_pos(b["t"], min(b["o"], take), "익절")
        # 3) 이번 봉 마감 신호 → 다음 봉에서 실행
        pending = bot.decide(f[i - 1], s[i - 1], f[i], s[i], r[i], p)
        mark = 0.0
        if pos:
            d = 1 if pos["side"] == "long" else -1
            mark = notional * d * (b["c"] / pos["entry"] - 1)
        curve.append((b["t"], total + mark))
    if pos:
        close_pos(bars[-1]["t"], bars[-1]["c"], "기간 끝")
        curve.append((bars[-1]["t"], total))
    return {"trades": trades, "curve": curve, "fees": fees}


def stats(res):
    t = res["trades"]
    n = len(t)
    total = sum(x[3] for x in t)
    gross = sum(x[4] for x in t)
    wins = sum(1 for x in t if x[3] > 0)
    peak, mdd = 0.0, 0.0
    for _, v in res["curve"]:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    half = (t[0][0] + t[-1][1]) / 2 if t else 0
    first = sum(x[3] for x in t if x[1] <= half)
    return {"n": n, "total": total, "gross": gross, "win": wins / n * 100 if n else 0.0,
            "mdd": mdd, "fees": res["fees"], "first": first, "second": total - first,
            "stops": sum(1 for x in t if x[5] == "손절"), "takes": sum(1 for x in t if x[5] == "익절")}


def period(bars):
    fmt = "%Y-%m-%d"
    return (f"{datetime.fromtimestamp(bars[0]['t']).strftime(fmt)} ~ "
            f"{datetime.fromtimestamp(bars[-1]['t']).strftime(fmt)} ({len(bars)}봉)")


def line(label, st, order_usdt):
    return (f"  {label:<22} 거래 {st['n']:>3}회 · 승률 {st['win']:>3.0f}% · 순손익 {st['total']:+8.2f} USDT"
            f" ({st['total'] / order_usdt * 100:+.0f}%) · 수수료 전 {st['gross']:+.2f} · 수수료 {st['fees']:.2f}"
            f" · 최대낙폭 {st['mdd']:.2f} · 앞/뒤 절반 {st['first']:+.1f}/{st['second']:+.1f}")


def hold_line(bars, market, p):
    chg = bars[-1]["c"] / bars[0]["o"] - 1
    if market == "spot":
        return f"  {'그냥 보유(같은 금액)':<22} 순손익 {p['order_usdt'] * chg:+8.2f} USDT ({chg * 100:+.0f}%)"
    return f"  {'(참고) 기간 가격 변화':<22} {chg * 100:+.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true", help="봉 간격 × EMA 조합 비교")
    ap.add_argument("--slip", type=float, default=0.05, help="슬리피지 편도 %%")
    a = ap.parse_args()
    p = dict(bot.CFG)
    gate = bot.Gate("", "")
    jobs = [("spot", s, p["spot_fee_pct"]) for s in p["spot_symbols"]] + \
           [("fut", s, p["fut_fee_pct"]) for s in p["futures_symbols"]]
    print(f"━━ Gate.io 봇 백테스트 — 1회 {p['order_usdt']:g} USDT, 선물 x{p['leverage']}, "
          f"손절 {p['stop_loss_pct']}% / 익절 {p['take_profit_pct']}%, 슬리피지 {a.slip}% ━━")
    print("   (현물 수수료 편도 %.2f%%, 선물 %.3f%%, 펀딩비 미반영)" % (p["spot_fee_pct"], p["fut_fee_pct"]))
    intervals = [p["interval"]] + ([i for i in ("15m", "1h", "4h") if i != p["interval"]] if a.grid else [])
    emas = [(p["ema_fast"], p["ema_slow"])] + ([e for e in ((9, 21), (20, 50), (50, 200))
                                                 if e != (p["ema_fast"], p["ema_slow"])] if a.grid else [])
    verdicts = []
    for market, sym, fee in jobs:
        name = ("현물 " if market == "spot" else "선물 ") + sym
        for iv in intervals:
            print(f"\n■ {name} · {iv}봉 받는 중…")
            try:
                bars = fetch(gate, market, sym, iv)
            except Exception as e:
                print(f"  ❌ 시세를 받지 못함: {bot.redact(e)[:120]}")
                continue
            if len(bars) < 300:
                print("  데이터 부족")
                continue
            print(f"  기간 {period(bars)}")
            print(hold_line(bars, market, p))
            for fe, sl in emas:
                q = dict(p, ema_fast=fe, ema_slow=sl)
                st = stats(simulate(bars, q, market, fee, a.slip))
                label = f"EMA{fe}/{sl}" + (" (현재 설정)" if (iv, fe, sl) == (p["interval"], p["ema_fast"], p["ema_slow"]) else "")
                print(line(label, st, p["order_usdt"]))
                if (iv, fe, sl) == (p["interval"], p["ema_fast"], p["ema_slow"]):
                    verdicts.append((name, st))
    print("\n━━ 현재 설정 판정 ━━")
    for name, st in verdicts:
        if st["n"] < 20:
            msg = "거래가 20회 미만이라 판단 불가 — 더 긴 봉(--grid)으로 확인"
        elif st["total"] > 0 and st["first"] > 0 and st["second"] > 0:
            msg = "✅ 수수료 후 이익, 앞·뒤 절반 모두 + (그래도 소액·모의로 먼저)"
        elif st["total"] > 0:
            msg = "△ 이익이지만 한쪽 기간에 몰림 — 운일 가능성"
        elif st["gross"] > 0:
            msg = "❌ 수수료 전엔 +, 수수료 때문에 손실 — 매매 횟수를 줄여야 함"
        else:
            msg = "❌ 수수료 전에도 손실 — 전략 자체가 이 기간엔 안 통함"
        print(f"  {name}: {msg}")
    print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다. 펀딩비·강제청산·호가 공백은 반영되지 않았습니다.")


if __name__ == "__main__":
    main()
