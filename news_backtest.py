"""
뉴스 반응 백테스트 — "호재 뉴스가 뜨고 N분 뒤에 사면 수수료 빼고 남는가?"를 과거 데이터로 확인

  venv/bin/python -u news_backtest.py                                     # 기본 목록, 최근 2년
  venv/bin/python -u news_backtest.py --targets @universes/sec_auto.txt --sample 60 --years 3
  venv/bin/python -u news_backtest.py --delays 0,1,5,15,60 --examples 20

.env 에 ALPACA_KEY_ID, ALPACA_SECRET_KEY 필요. 뉴스(Benzinga 제공)와 1분봉은 data/alpaca/ 에 저장해 재사용.

방법
  1. 종목별 과거 뉴스 제목을 키워드 규칙으로 호재/악재/중립 판정 (AI 판정 아님 — 공짜·빠름·거침)
     · 시세 움직임을 '사후에' 전하는 기사(…shares are trading higher 등)와 여러 종목을 묶은 기사는 제외
     · 같은 종목·같은 날은 첫 기사 한 건만 (겹치는 매매 방지)
  2. 기사 시각 + 지연(분) 뒤 첫 1분봉 시가에 매수 (장 시작 전·마감 후 기사는 다음 정규장 시작가)
  3. 청산 4가지: 30분 뒤 / 당일 종가 / 다음 날 시가 / 5거래일 뒤 종가
  4. '진입 전 이미 움직임' = 기사 직전 가격 → 실제 매수 가격. 지연이 늘수록 놓치는 몫
  악재 뉴스도 같은 방식으로 계산해 대조군으로 보여줌 (호재·악재가 비슷하면 판정이 의미 없음)

한계: 실제 뉴스 수신·판정·한투 주문까지 걸리는 시간, 장 전·후 주문 제약, 소형주 호가 공백은 반영 못 함
"""

import argparse
import json
import os
import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from minute_backtest import ET, AlpacaData, by_day

HERE = Path(__file__).resolve().parent
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

POSITIVE = {  # 분류: 제목에 들어 있으면 호재 후보 (소문자 기준, 정규식)
    "실적": [r"\bbeats?\b", r"\btops?\b.*estimates", r"above (consensus|estimates|expectations)",
             r"raises? (fy|full[- ]year|annual|q\d)?.*(guidance|outlook|forecast)", r"record (revenue|sales|quarter)"],
    "승인·임상": [r"fda (approv|clear|grants)", r"\bapproval\b", r"positive (topline|results|data)",
               r"met (its )?primary endpoint", r"breakthrough (therapy|device) designation"],
    "인수·합병": [r"to be acquired", r"agrees? to be acquired", r"buyout", r"takeover (offer|bid)",
              r"\bacquired by\b"],
    "투자의견": [r"upgrade[sd]?\b", r"initiates? .*\b(buy|outperform|overweight)\b",
             r"(raises?|boosts?) (price target|pt)\b", r"price target raised"],
    "계약·제휴": [r"awarded", r"wins? .*(contract|order|deal)", r"(strategic )?partnership with",
              r"selected by", r"multi[- ]year (contract|agreement)"],
    "주주환원": [r"(buyback|repurchase) (program|plan|authorization)", r"(raises|increases|hikes) (quarterly )?dividend"],
}
NEGATIVE = [r"\bmiss(es|ed)?\b", r"below (consensus|estimates|expectations)",
            r"(cuts?|lowers?|reduces?|withdraws?) .*(guidance|outlook|forecast)", r"downgrade[sd]?\b",
            r"(lowers?|cuts?) (price target|pt)\b", r"price target (lowered|cut)",
            r"(public|stock|share|equity|secondary) offering", r"\bdilut", r"investigation", r"lawsuit",
            r"\bsues?\b", r"\brecall", r"\bdelay", r"complete response letter", r"fda rejects?",
            r"bankruptcy", r"going concern", r"\bhalts?\b", r"short seller", r"\bsec probe", r"layoffs?"]
AFTER_THE_FACT = [r"shares? (are|is) (trading|moving|down|up|higher|lower)", r"stocks? moving",
                  r"\bmovers\b", r"trading (higher|lower)", r"here'?s why", r"why .* (stock|shares)",
                  r"(gap|gapping) (up|down)", r"mid-day|pre-market|after-hours", r"\bwhat'?s going on\b",
                  r"\b(soar|soars|soaring|jumps?|surges?|spikes?|plunges?|tumbles?|sinks?|falls?)\b"]
_POS = [(cat, re.compile(p)) for cat, ps in POSITIVE.items() for p in ps]
_NEG = [re.compile(p) for p in NEGATIVE]
_AFT = [re.compile(p) for p in AFTER_THE_FACT]
EXITS = ["30분", "당일종가", "다음날시가", "5일뒤종가"]


def classify(headline):
    """제목 → ("호재", 분류) / ("악재", "") / (None, "") — 호재·악재 키워드가 함께 있으면 판정 안 함"""
    h = headline.lower()
    if any(p.search(h) for p in _AFT):
        return None, ""
    neg = any(p.search(h) for p in _NEG)
    pos = next((cat for cat, p in _POS if p.search(h)), None)
    if pos and not neg:
        return "호재", pos
    if neg and not pos:
        return "악재", ""
    return None, ""


# ─── 뉴스 데이터 ──────────────────────────────────────
class AlpacaNews(AlpacaData):
    """Alpaca 과거 뉴스 — 종목·월 단위로 받아 JSON 저장 (지난 달은 다시 받지 않음)"""

    def news_month(self, sym, y, m, today=None):
        today = today or date.today()
        path = self.cache / "news" / sym / f"{y:04d}-{m:02d}.json"
        done = (y, m) < (today.year, today.month)
        if done and path.exists():
            return json.loads(path.read_text())
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = min(date(ny, nm, 1), today + timedelta(days=1))
        items, token = [], None
        while True:
            params = {"symbols": sym, "start": f"{y:04d}-{m:02d}-01T00:00:00Z", "end": f"{end.isoformat()}T00:00:00Z",
                      "limit": 50, "sort": "asc", "include_content": "false"}
            if token:
                params["page_token"] = token
            data = self._get(NEWS_URL, params)
            items += [{"t": n["created_at"], "h": n.get("headline", ""), "s": n.get("symbols", [])}
                      for n in data.get("news") or []]
            token = data.get("next_page_token")
            if not token:
                break
        if done:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items))
        return items

    def news(self, sym, years, today=None):
        today = today or date.today()
        y, m, out = today.year - years, today.month, []
        while (y, m) <= (today.year, today.month):
            out += self.news_month(sym, y, m, today)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out


def to_et(ts):
    return datetime.strptime(ts.replace("Z", "+0000")[:19] + "+0000", "%Y-%m-%dT%H:%M:%S%z").astimezone(ET)


def events(sym, items, max_syms=3):
    """뉴스 → [(뉴욕시각, 판정, 분류, 제목)] — 종목·날짜·판정별 첫 기사만"""
    out, seen = [], set()
    for n in sorted(items, key=lambda n: n["t"]):
        if len(n["s"]) > max_syms or sym not in n["s"]:
            continue
        label, cat = classify(n["h"])
        if not label:
            continue
        t = to_et(n["t"])
        key = (t.date(), label)
        if key in seen:
            continue
        seen.add(key)
        out.append((t, label, cat, n["h"]))
    return out


# ─── 반응 계산 ────────────────────────────────────────
def _hm(t):
    return t.strftime("%H:%M")


def measure(days, t, delay, fee_pct=0.25, slip_pct=0.05):
    """기사 시각 t, 지연 delay분 → {"moved": 진입 전 움직임, "night": 장외 기사 여부, 청산별 순수익}. 데이터 없으면 None
    days: by_day() 결과 [(YYYYMMDD, [1분봉…])]"""
    fee, slip = fee_pct / 100, slip_pct / 100
    target = t + timedelta(minutes=delay, seconds=59)  # 기사 다음 '완성된' 분부터
    tday, thm = target.strftime("%Y%m%d"), _hm(target)
    di = next((i for i, (d, _) in enumerate(days) if d >= t.strftime("%Y%m%d")), None)
    if di is None:
        return None
    d, bars = days[di]
    entry = None
    if d == tday:
        entry = next(((j, b) for j, b in enumerate(bars) if b["time"] >= thm), None)
    elif d > tday:  # 주말·휴일 또는 장 전 기사 → 그날 첫 봉
        entry = (0, bars[0])
    if entry is None:  # 장 마감 뒤 → 다음 거래일 첫 봉
        di += 1
        if di >= len(days):
            return None
        d, bars = days[di]
        entry = (0, bars[0])
    j, eb = entry
    night = j == 0 and not (d == t.strftime("%Y%m%d") and "09:30" <= _hm(t) <= "15:59")
    before = [b for b in bars[:j] if b["time"] < _hm(t)] if d == t.strftime("%Y%m%d") else []
    pre = before[-1]["close"] if before else (days[di - 1][1][-1]["close"] if di > 0 else None)
    price = eb["open"]
    if not pre or not price:
        return None
    buy = price * (1 + slip)
    ex = {}
    end30 = (datetime.strptime(eb["time"], "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
    b30 = next((b for b in bars[j:] if b["time"] >= end30), None)
    ex["30분"] = b30["open"] if b30 else bars[-1]["close"]
    ex["당일종가"] = bars[-1]["close"]
    if di + 1 < len(days):
        ex["다음날시가"] = days[di + 1][1][0]["open"]
    if di + 5 < len(days):
        ex["5일뒤종가"] = days[di + 5][1][-1]["close"]
    out = {"moved": price / pre - 1, "night": night}
    for k, v in ex.items():
        out[k] = v * (1 - slip) * (1 - fee) * (1 - fee) / buy - 1
    return out


def months_needed(evts, extra_days=10):
    """이벤트 날짜 + 며칠 뒤까지 포함하는 (연, 월) 목록"""
    need = set()
    for t, *_ in evts:
        for k in (0, extra_days):
            d = t.date() + timedelta(days=k)
            need.add((d.year, d.month))
    return sorted(need)


# ─── 집계·출력 ────────────────────────────────────────
def _avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _med(xs):
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


def summarize(rows, title):
    """rows: [{"moved", 청산별 수익…}] → 한 줄씩"""
    if not rows:
        print(f"  {title}: 해당 없음")
        return
    parts = []
    for k in EXITS:
        xs = [r[k] for r in rows if k in r]
        if xs:
            win = sum(x > 0 for x in xs) / len(xs) * 100
            parts.append(f"{k} {_avg(xs) * 100:+.2f}%(중앙 {_med(xs) * 100:+.2f}, 승 {win:.0f}%)")
    moved = _avg([r["moved"] for r in rows]) * 100
    print(f"  {title:<16} {len(rows):>4}건 | 진입 전 이미 {moved:+.2f}% | " + " | ".join(parts))


def report(results, delays, examples=10):
    """results: [(종목, 시각, 판정, 분류, 제목, {지연: measure 결과})]"""
    print(f"\n━━ 뉴스 반응 백테스트 ({len({r[0] for r in results})}종목, 기사 {len(results)}건) — 수수료·슬리피지 차감 후 1회 평균 ━━")
    for label in ("호재", "악재"):
        print(f"\n■ {label} 뉴스")
        for dl in delays:
            rows = [r[5][dl] for r in results if r[2] == label and r[5].get(dl)]
            summarize(rows, f"{dl}분 뒤 매수")
    base = delays[min(1, len(delays) - 1)]
    print(f"\n■ 호재 — 장중 기사 vs 장외 기사 ({base}분 뒤 매수)")
    pos = [r for r in results if r[2] == "호재" and r[5].get(base)]
    summarize([r[5][base] for r in pos if not r[5][base]["night"]], "장중(바로 매수)")
    summarize([r[5][base] for r in pos if r[5][base]["night"]], "장외(다음 시가)")
    print(f"\n■ 호재 분류별 ({base}분 뒤 매수)")
    for cat in POSITIVE:
        summarize([r[5][base] for r in pos if r[3] == cat], cat)
    if examples:
        print(f"\n■ 호재 판정 예시 {min(examples, len(pos))}건 (판정이 그럴듯한지 눈으로 확인용, 무작위)")
        for r in random.Random(1).sample(pos, min(examples, len(pos))):
            m = r[5][base]
            print(f"  {r[1]:%Y-%m-%d %H:%M} {r[0]:<5} [{r[3]}] 당일종가 {m['당일종가'] * 100:+.1f}% | {r[4][:80]}")
    print("\n읽는 법: '진입 전 이미'가 클수록 늦게 산 것. 호재 줄의 수익이 악재·0 과 뚜렷이 다르고,")
    print("        지연을 늘려도 + 가 유지돼야 실전(수십 초 이상 지연)에서도 기대할 만함")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--targets", default="@universes/growth_balanced.txt")
    p.add_argument("--sample", type=int, default=0, help="목록에서 무작위 N종목만 (0=전부)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--years", type=int, default=2)
    p.add_argument("--delays", default="0,1,5,15", help="뉴스 후 매수까지 지연(분), 쉼표로")
    p.add_argument("--max-syms", type=int, default=3, help="기사에 묶인 종목이 이보다 많으면 제외")
    p.add_argument("--feed", default="sip", choices=["sip", "iex"])
    p.add_argument("--fee", type=float, default=None)
    p.add_argument("--slip", type=float, default=0.05)
    p.add_argument("--examples", type=int, default=10)
    a = p.parse_args()

    load_dotenv(HERE / ".env")
    g = os.environ.get
    from kis_api import parse_targets
    syms = sorted(parse_targets(a.targets))
    if a.sample:
        syms = sorted(random.Random(a.seed).sample(syms, min(a.sample, len(syms))))
    delays = [int(x) for x in a.delays.split(",") if x.strip()]
    fee = a.fee if a.fee is not None else float(g("FEE_PCT", "0.25"))
    api = AlpacaNews(g("ALPACA_KEY_ID"), g("ALPACA_SECRET_KEY"), a.feed)
    results = []
    for i, sym in enumerate(syms, 1):
        try:
            evts = events(sym, api.news(sym, a.years), a.max_syms)
            rows = []
            for y, m in months_needed(evts):  # 이벤트가 있는 달의 1분봉만 받음 (메모리·시간 절약)
                rows += api.month(sym, "1Min", y, m)
        except RuntimeError as e:
            print(f"[{i}/{len(syms)}] {sym}: ❌ {e}")
            continue
        days = by_day(rows)
        n = 0
        for t, label, cat, head in evts:
            res = {dl: measure(days, t, dl, fee, a.slip) for dl in delays}
            if any(res.values()):
                results.append((sym, t, label, cat, head, res))
                n += 1
        pos = sum(1 for e in evts if e[1] == "호재")
        print(f"[{i}/{len(syms)}] {sym}: 판정된 기사 {len(evts)}건 (호재 {pos}, 악재 {len(evts) - pos}) → 계산 {n}건")
    if not results:
        print("계산된 기사가 없습니다 — 종목·기간을 늘려 보세요")
        return
    report(results, delays, a.examples)
    print("\n※ 과거 성과가 미래 수익을 보장하지 않습니다.")


if __name__ == "__main__":
    main()
