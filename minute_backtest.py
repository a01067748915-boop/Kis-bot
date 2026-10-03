"""
분봉 정밀 백테스트 — Alpaca 과거 분봉(수년치)으로 장중 전략을 분 단위로 재현

  venv/bin/python minute_backtest.py --targets SOXX,QQQM --years 3
  venv/bin/python minute_backtest.py --targets @universes/growth_balanced.txt --years 2 --tf 5Min
  venv/bin/python minute_backtest.py --fetch-only --targets NVDA,AMD --years 5     # 데이터만 미리 받기

.env 에 ALPACA_KEY_ID, ALPACA_SECRET_KEY 필요 (무료 계정 가능). 받은 분봉은 data/alpaca/ 에 저장해 재사용.

일봉 백테스트와 달리 분 단위 가격 순서를 그대로 따라가므로
  - 목표가 돌파 시각, 손절이 매수 뒤에 걸렸는지, 청산 시각 가격을 정확히 반영
  - 비용 전/후 1회 평균 수익을 나눠 보여줘 '전략 자체의 힘'과 '수수료 부담'을 구분

전략 (모두 매수만, 당일 청산)
  vb  : 변동성 돌파 — 목표가 = 시가 + K × 전일 변동폭, 장중 돌파 시 매수, 손절 또는 청산 시각에 매도 (원래 봇 전략)
  orb : 시가 범위 돌파 — 장 시작 N분의 최고가 돌파 시 매수, 그 범위 최저가 이탈 시 손절, 청산 시각에 매도
"""

import argparse
import csv
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2/stocks/{sym}/bars"


# ─── 데이터 ───────────────────────────────────────────
class AlpacaData:
    """Alpaca 과거 분봉 — 월 단위로 받아 CSV 캐시 (지난 달은 다시 받지 않음)"""

    def __init__(self, key, secret, feed="sip", cache=HERE / "data" / "alpaca", session=None):
        if not key or not secret:
            raise ValueError(".env 에 ALPACA_KEY_ID, ALPACA_SECRET_KEY 를 넣으세요")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.feed = feed
        self.cache = Path(cache)
        self.http = session or requests.Session()

    def _get(self, url, params):
        for attempt in range(5):
            res = self.http.get(url, headers=self.headers, params=params, timeout=30)
            if res.status_code == 429:  # 호출 한도 → 잠시 대기
                time.sleep(2 + attempt * 3)
                continue
            if res.status_code == 403 and params.get("feed") == "sip":
                raise PermissionError("sip")
            if res.status_code != 200:
                raise RuntimeError(f"Alpaca {res.status_code}: {res.text[:200]}")
            return res.json()
        raise RuntimeError("Alpaca 호출 한도 초과가 계속됨 — 잠시 후 다시 실행하세요")

    def _download(self, sym, tf, start, end):
        bars, token = [], None
        while True:
            params = {"timeframe": tf, "start": start, "end": end, "limit": 10000,
                      "adjustment": "all", "feed": self.feed}
            if token:
                params["page_token"] = token
            try:
                data = self._get(DATA_URL.format(sym=sym), params)
            except PermissionError:
                print("  ℹ️ 무료 계정이라 전체 시장(sip) 데이터 권한이 없어 IEX 데이터로 바꿉니다 (거래량이 작게 잡힘)")
                self.feed = "iex"
                continue
            bars += data.get("bars") or []
            token = data.get("next_page_token")
            if not token:
                return bars

    def month(self, sym, tf, y, m, today=None):
        today = today or date.today()
        path = self.cache / tf / sym / f"{y:04d}-{m:02d}.csv"
        done = (y, m) < (today.year, today.month)
        if done and path.exists():
            return read_csv(path)
        start = f"{y:04d}-{m:02d}-01T00:00:00Z"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end_d = min(date(ny, nm, 1), today)
        rows = [to_row(b) for b in self._download(sym, tf, start, f"{end_d.isoformat()}T00:00:00Z")]
        rows = [r for r in rows if r]
        if done:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_csv(path, rows)
        return rows

    def bars(self, sym, tf, years, today=None):
        today = today or date.today()
        y, m = today.year - years, today.month
        out = []
        while (y, m) <= (today.year, today.month):
            out += self.month(sym, tf, y, m, today)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out


def to_row(b):
    """Alpaca 봉 → 정규장(뉴욕 09:30~15:59)만 {date, time, open, high, low, close, volume}"""
    t = datetime.strptime(b["t"].replace("Z", "+0000")[:19] + "+0000", "%Y-%m-%dT%H:%M:%S%z").astimezone(ET)
    hm = t.strftime("%H:%M")
    if not ("09:30" <= hm <= "15:59"):
        return None
    return {"date": t.strftime("%Y%m%d"), "time": hm, "open": float(b["o"]), "high": float(b["h"]),
            "low": float(b["l"]), "close": float(b["c"]), "volume": float(b.get("v", 0))}


FIELDS = ["date", "time", "open", "high", "low", "close", "volume"]


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return [{k: (v if k in ("date", "time") else float(v)) for k, v in r.items()} for r in csv.DictReader(f)]


def write_csv(path, rows):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def by_day(rows):
    """분봉 → [(날짜, [봉…])] 날짜순"""
    days = {}
    for r in rows:
        days.setdefault(r["date"], []).append(r)
    return [(d, sorted(days[d], key=lambda r: r["time"])) for d in sorted(days)]


def daily(bars):
    return {"open": bars[0]["open"], "high": max(b["high"] for b in bars),
            "low": min(b["low"] for b in bars), "close": bars[-1]["close"]}


# ─── 장중 매매 재현 ────────────────────────────────────
def run_trade(bars, entry_level, stop_level, entry_from, entry_to, exit_at, chase_pct):
    """entry_from~entry_to 사이 처음 entry_level 을 넘는 봉에서 매수 → 손절/청산 시각까지 분 단위 추적
    → (매수가, 매도가, 사유) 또는 None. 가격은 슬리피지 반영 전"""
    entry = None
    for i, b in enumerate(bars):
        if entry is None:
            if b["time"] < entry_from or b["time"] > entry_to:
                continue
            if b["high"] < entry_level:
                continue
            px = max(entry_level, b["open"])  # 갭으로 이미 위면 그 봉 시가
            if px > entry_level * (1 + chase_pct / 100):
                return None  # 너무 올라 추격 안 함 (하루 1회)
            entry = px
            # 매수한 그 봉 안: 시가부터 이미 매수선 위(갭)였으면 저가는 매수 뒤 → 손절선 아래면 손절.
            # 아래에서 올라와 돌파한 봉이면 저가는 돌파 전에 나온 것으로 봄 (1분봉이면 오차 작음)
            if b["low"] <= stop_level and b["open"] >= entry_level:
                return entry, stop_level, "손절"
            continue
        if b["time"] >= exit_at:
            return entry, b["open"], "청산"
        if b["low"] <= stop_level:
            return entry, min(stop_level, b["open"]), "손절"
    if entry is not None:
        return entry, bars[-1]["close"], "종가"
    return None


def simulate(days, strategy, k=0.5, ma=5, stop_pct=2.0, orb_min=15, chase_pct=1.0, entry_from="09:35",
             entry_to="15:00", exit_at="15:45", fee_pct=0.25, slip_pct=0.05):
    """한 종목 하루 1회 장중 매매 → {"curve", "trades"[(비용후, 비용전, 사유, 날짜)], "dates"}"""
    fee, slip = fee_pct / 100, slip_pct / 100
    eq, curve, trades, dates = 1.0, [], [], []
    dly = [daily(b) for _, b in days]
    for i in range(1, len(days)):
        d, bars = days[i]
        y = dly[i - 1]
        if strategy == "vb":
            if ma and (i < ma or bars[0]["open"] <= sum(x["close"] for x in dly[i - ma:i]) / ma):
                curve.append(eq)
                dates.append(d)
                continue
            level = bars[0]["open"] + k * (y["high"] - y["low"])
            res = run_trade(bars, level, level * (1 - stop_pct / 100), entry_from, entry_to, exit_at, chase_pct)
        else:  # orb
            end_t = (datetime.strptime("09:30", "%H:%M") + timedelta(minutes=orb_min)).strftime("%H:%M")
            rng = [b for b in bars if b["time"] < end_t]
            if not rng:
                curve.append(eq)
                dates.append(d)
                continue
            hi, lo = max(b["high"] for b in rng), min(b["low"] for b in rng)
            res = run_trade(bars, hi, lo, end_t, entry_to, exit_at, chase_pct)
        if res:
            buy, sell, why = res
            gross = sell / buy - 1
            net = (sell * (1 - slip) * (1 - fee)) / (buy * (1 + slip) / (1 - fee)) - 1
            eq *= 1 + net
            trades.append((net, gross, why, d))
        curve.append(eq)
        dates.append(d)
    return {"curve": curve, "trades": trades, "dates": dates}


# ─── 결과 ─────────────────────────────────────────────
def stats(curve, days=None):
    peak, mdd = 1.0, 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    end = curve[-1] if curve else 1.0
    n = days or len(curve)
    cagr = end ** (252 / n) - 1 if n and end > 0 else -1.0
    return (end - 1) * 100, cagr * 100, mdd * 100


def line(name, r):
    t = r["trades"]
    n = len(t)
    tot, cagr, mdd = stats(r["curve"])
    if not n:
        return f"  {name:<16}: 매매 없음"
    net = sum(x[0] for x in t) / n * 100
    gross = sum(x[1] for x in t) / n * 100
    win = sum(x[0] > 0 for x in t) / n * 100
    stops = sum(x[2] == "손절" for x in t)
    return (f"  {name:<16}: 수익 {tot:+7.1f}% (연 {cagr:+5.1f}%), 최대낙폭 {mdd:6.1f}%, 매매 {n}회(손절 {stops}),"
            f" 승률 {win:.0f}%, 1회평균 비용전 {gross:+.2f}% → 비용후 {net:+.2f}%")


VARIANTS = [
    ("변동성돌파 K0.5", "vb", {"k": 0.5}), ("변동성돌파 K0.3", "vb", {"k": 0.3}),
    ("시가범위 5분", "orb", {"orb_min": 5}), ("시가범위 15분", "orb", {"orb_min": 15}),
    ("시가범위 30분", "orb", {"orb_min": 30}),
]


def report(data, opts):
    print(f"\n수수료 편도 {opts['fee_pct']}%, 슬리피지 편도 {opts['slip_pct']}%, 손절 -{opts['stop_pct']}%"
          f"(변동성돌파), 청산 {opts['exit_at']} (뉴욕)")
    combined = {name: [] for name, *_ in VARIANTS}
    detail = len(data) <= 10  # 종목이 많으면 합산만
    holds = []
    for sym, days in data.items():
        if len(days) < 30:
            print(f"\n■ {sym}: 데이터 부족 ({len(days)}일)")
            continue
        first, last = days[0][1][0], days[-1][1][-1]
        hold = last["close"] / first["open"] - 1
        holds.append(hold)
        if detail:
            print(f"\n■ {sym} {days[0][0]}~{days[-1][0]} ({len(days)}일) — 그냥 보유 {hold * 100:+.1f}%")
        for name, strat, kw in VARIANTS:
            r = simulate(days, strat, **{**opts, **kw})
            combined[name].append(r)
            if detail:
                print(line(name, r))
    print(f"\n■ 전체 {len(holds)}종목 균등 배분 (종목마다 같은 금액, 각자 운용)")
    if holds:
        print(f"  {'그냥 보유':<16}: 수익 {sum(holds) / len(holds) * 100:+7.1f}% (기간 전체, 균등)")
    for name, rs in combined.items():
        if not rs:
            continue
        dates = sorted(set().union(*(r["dates"] for r in rs)))
        lastv = [1.0] * len(rs)
        maps = [dict(zip(r["dates"], r["curve"])) for r in rs]
        curve = []
        for d in dates:
            for i, m in enumerate(maps):
                lastv[i] = m.get(d, lastv[i])
            curve.append(sum(lastv) / len(rs))
        trades = [t for r in rs for t in r["trades"]]
        mark = ""
        if trades:
            net = sum(x[0] for x in trades) / len(trades) * 100
            mark = " ✅" if net > 0.3 else (" △" if net > 0 else " ⚠️")
        print(line(name, {"curve": curve, "trades": trades}) + mark)
    print("\n※ 비용전 1회평균이 비용(약 0.6%)보다 작으면 전략 자체에 힘이 있어도 수수료에 먹힙니다.")
    print("※ 과거 성과가 미래 수익을 보장하지 않습니다.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--targets", default=None, help="SOXX,QQQM 또는 @universes/파일 (기본: .env 의 TARGETS)")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--tf", default="1Min", choices=["1Min", "5Min", "15Min"], help="분봉 단위 (종목 많으면 5Min)")
    p.add_argument("--feed", default="sip", choices=["sip", "iex"])
    p.add_argument("--fee", type=float, default=None)
    p.add_argument("--slip", type=float, default=0.05)
    p.add_argument("--stop", type=float, default=None)
    p.add_argument("--fetch-only", action="store_true", help="데이터만 받아 저장")
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    from kis_api import parse_targets
    syms = list(parse_targets(a.targets or g("TARGETS", "QQQM:NAS,SOXX:NAS")))
    api = AlpacaData(g("ALPACA_KEY_ID"), g("ALPACA_SECRET_KEY"), a.feed)
    data = {}
    for sym in syms:
        print(f"{sym} {a.tf} 분봉 받는 중… (처음엔 오래 걸리고, 이후엔 저장분 재사용)")
        try:
            data[sym] = by_day(api.bars(sym, a.tf, a.years))
        except RuntimeError as e:
            print(f"  ❌ {sym}: {e}")
    if a.fetch_only:
        print("저장 완료:", HERE / "data" / "alpaca")
        return
    opts = {"fee_pct": a.fee if a.fee is not None else float(g("FEE_PCT", "0.25")), "slip_pct": a.slip,
            "stop_pct": a.stop if a.stop is not None else float(g("STOP_LOSS_PCT", "2")),
            "exit_at": g("EXIT_TIME", "15:45")}
    report(data, opts)


if __name__ == "__main__":
    main()
