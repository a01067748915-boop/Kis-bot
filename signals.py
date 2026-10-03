"""눌림목 매수 신호 계산 — 봇(bot.py)과 백테스트(strategies.py)가 같은 코드를 씀"""


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


def indicators(bars, trend_ma=200, exit_ma=5):
    """날짜별 {rsi, trend(종가>추세선), recovered(종가>단기선)} — 그날 종가까지로 계산"""
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


def market_indicators(bars, trend_ma=200, vol_ma=20, vol_mult=1.5, window=3):
    """시장 대용 종목(SPY 등) 날짜별 {trend, rsi, vol_ratio, panic, panic_recent} — 그날 종가까지로 계산
    panic: 하락 마감 + 거래량이 직전 vol_ma일 평균의 vol_mult배 이상 (투매)
    panic_recent: 최근 window 거래일(그날 포함) 안에 panic 이 있었음"""
    closes = [b["close"] for b in bars]
    vols = [b.get("volume") or 0 for b in bars]
    r = rsi(closes, 2)
    out, panics = {}, []
    for i, b in enumerate(bars):
        avg_vol = sum(vols[i - vol_ma:i]) / vol_ma if i >= vol_ma else 0
        ratio = vols[i] / avg_vol if avg_vol else 0
        panic = i > 0 and closes[i] < closes[i - 1] and ratio >= vol_mult
        panics.append(panic)
        if i + 1 < max(trend_ma, vol_ma + 1) or r[i] is None:
            continue
        out[b["date"]] = {"trend": closes[i] > sum(closes[i + 1 - trend_ma:i + 1]) / trend_ma,
                          "rsi": r[i], "vol_ratio": ratio, "panic": panic,
                          "panic_recent": any(panics[max(0, i + 1 - window):i + 1])}
    return out


def breakout_indicators(bars, entry_n=55, exit_n=20, trend_ma=200, mom_n=126):
    """스윙 돌파 날짜별 {entry, exit, trend, mom} — 그날 종가까지로 계산
    entry: 종가가 직전 entry_n일 최고 종가 돌파 / exit: 직전 exit_n일 최저 종가 아래로
    trend: 종가 > trend_ma일선 (0이면 항상 True) / mom: 최근 mom_n일 수익률(우선순위용)"""
    closes = [b["close"] for b in bars]
    need = max(entry_n, exit_n, trend_ma, mom_n)
    out = {}
    for i, b in enumerate(bars):
        if i < need:
            continue
        c = closes[i]
        out[b["date"]] = {"entry": c > max(closes[i - entry_n:i]), "exit": c < min(closes[i - exit_n:i]),
                          "trend": not trend_ma or c > sum(closes[i + 1 - trend_ma:i + 1]) / trend_ma,
                          "mom": c / closes[i - mom_n] - 1}
    return out


# ─── 기술적 지표 (교과서 기본값) ──────────────────────
def ema(values, n):
    """지수이동평균 — 첫 값으로 시작"""
    out, a = [], 2 / (n + 1)
    for i, v in enumerate(values):
        out.append(v if i == 0 else out[-1] + a * (v - out[-1]))
    return out


def _fresh(state, i, k=5):
    """state[i] 가 참이고 최근 k일 안에 거짓→참으로 바뀌었음 (오래전 신호에 늦게 올라타지 않게)"""
    return state[i] and any(not state[j] for j in range(max(0, i - k), i))


def _base(closes, i, trend_ma=200, mom_n=126):
    return {"trend": closes[i] > sum(closes[i + 1 - trend_ma:i + 1]) / trend_ma,
            "mom": closes[i] / closes[i - mom_n] - 1}


def bollinger_indicators(bars, mode="revert", n=20, k=2.0, squeeze_n=126, trend_ma=200, mom_n=126):
    """볼린저 밴드 날짜별 {entry, exit, trend, mom}
    revert : 종가가 하단 밴드 아래 → 매수 / 중심선(n일선) 위로 → 매도 (200일선 위 종목만 사는 건 호출 쪽 trend)
    squeeze: 밴드 폭이 최근 squeeze_n일 최저의 1.1배 이내였다가 상단 밴드 돌파 → 매수 / 중심선 아래 → 매도"""
    closes = [b["close"] for b in bars]
    mid, up, lo, width = [], [], [], []
    for i in range(len(closes)):
        w = closes[max(0, i + 1 - n):i + 1]
        m = sum(w) / len(w)
        sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
        mid.append(m)
        up.append(m + k * sd)
        lo.append(m - k * sd)
        width.append((up[-1] - lo[-1]) / m if m else 0)
    need = max(n, trend_ma, mom_n, squeeze_n + 1)
    out = {}
    for i, b in enumerate(bars):
        if i < need:
            continue
        c = closes[i]
        if mode == "revert":
            entry = c < lo[i]
        else:
            entry = c > up[i] and width[i - 1] <= 1.1 * min(width[i - squeeze_n:i])
        out[b["date"]] = {"entry": entry, "exit": c > mid[i] if mode == "revert" else c < mid[i],
                          **_base(closes, i, trend_ma, mom_n)}
    return out


def macd_indicators(bars, weekly=False, fast=12, slow=26, sig=9, trend_ma=200, mom_n=126):
    """MACD 날짜별 {entry, exit, trend, mom}
    매수: MACD선 > 시그널선 이 최근 5일 안에 새로 시작 + MACD > 0 / 매도: MACD선 < 시그널선
    weekly=True 면 주봉(주 마지막 거래일 종가)으로 계산, 각 날짜엔 그날까지 끝난 주의 값을 씀"""
    closes = [b["close"] for b in bars]
    if weekly:
        wk_idx = []  # 주 마지막 거래일 인덱스
        for i, b in enumerate(bars):
            nxt = bars[i + 1]["date"] if i + 1 < len(bars) else None
            wk = _week(b["date"])
            if nxt is None or _week(nxt) != wk:
                wk_idx.append(i)
        wc = [closes[i] for i in wk_idx]
        m = [a - b for a, b in zip(ema(wc, fast), ema(wc, slow))]
        s = ema(m, sig)
        above = [x > y for x, y in zip(m, s)]
        pos = [x > 0 for x in m]
        day_state, day_pos, w = [], [], -1
        for i in range(len(bars)):
            while w + 1 < len(wk_idx) and wk_idx[w + 1] <= i:
                w += 1
            day_state.append(w >= slow and above[w])
            day_pos.append(w >= slow and pos[w])
        state, posv, warm = day_state, day_pos, 0
    else:
        m = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
        s = ema(m, sig)
        state = [x > y for x, y in zip(m, s)]
        posv = [x > 0 for x in m]
        warm = slow + sig
    need = max(warm, trend_ma, mom_n, 6)
    out = {}
    for i, b in enumerate(bars):
        if i < need:
            continue
        # 주봉은 신호가 주 1회만 바뀌므로 '새로 시작' 판단 기간을 주 단위(25거래일)로
        out[b["date"]] = {"entry": _fresh(state, i, 25 if weekly else 5) and posv[i], "exit": not state[i],
                          **_base(closes, i, trend_ma, mom_n)}
    return out


def _week(d):
    from datetime import date
    return date(int(d[:4]), int(d[4:6]), int(d[6:8])).isocalendar()[:2]


def ichimoku_indicators(bars, tenkan=9, kijun=26, span_b=52, trend_ma=200, mom_n=126):
    """일목균형표 날짜별 {entry, exit, trend, mom}
    매수: 종가가 구름 위 + 전환선 > 기준선 + 후행스팬(오늘 종가) > 26일 전 종가, 이 조건이 최근 5일 안에 새로 성립
    매도: 종가가 기준선 아래로. 구름(선행스팬)은 26일 전에 계산된 값"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]

    def mid(i, n):
        return (max(highs[i + 1 - n:i + 1]) + min(lows[i + 1 - n:i + 1])) / 2

    need = max(span_b + kijun, trend_ma, mom_n) + 6
    conv, base, state = {}, {}, [False] * len(bars)
    for i in range(span_b - 1, len(bars)):
        conv[i], base[i] = mid(i, tenkan), mid(i, kijun)
    for i in range(span_b - 1 + kijun, len(bars)):
        j = i - kijun  # 구름은 26일 전 값
        cloud = max((conv[j] + base[j]) / 2, mid(j, span_b))
        state[i] = closes[i] > cloud and conv[i] > base[i] and closes[i] > closes[i - kijun]
    out = {}
    for i, b in enumerate(bars):
        if i < need:
            continue
        out[b["date"]] = {"entry": _fresh(state, i), "exit": closes[i] < base[i], **_base(closes, i, trend_ma, mom_n)}
    return out
