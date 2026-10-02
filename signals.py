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
