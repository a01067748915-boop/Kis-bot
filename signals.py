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
