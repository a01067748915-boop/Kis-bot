"""
기본적 분석 데이터 — 미국 SEC EDGAR 재무제표 (무료, 키 불필요)

  - 기업이 제출한 분기(10-Q)·연간(10-K) 재무 수치를 '제출일'과 함께 받음
  - 백테스트의 각 날짜에는 그날까지 제출된 숫자만 사용 → 미래 정보가 섞이지 않음
  - SEC 규정상 요청마다 연락처(User-Agent)가 필요: .env 의 SEC_USER_AGENT="이름 이메일"
  - 받은 자료는 data/sec/ 에 저장해 7일간 재사용

지표
  growth : 매출 성장률 — 최근 4분기 매출 합 ÷ 그 전 4분기 합 − 1
  roe    : 자기자본이익률 — 최근 4분기 순이익 합 ÷ 최근 자기자본
"""

import json
import time
from datetime import date
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# 기업마다·시기마다 매출 항목 이름이 달라 여러 개를 합침 (앞쪽 우선)
REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet"]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]


class Edgar:
    def __init__(self, user_agent, cache=HERE / "data" / "sec", session=None, max_age_days=7):
        if not user_agent or "@" not in user_agent:
            raise ValueError('.env 에 SEC_USER_AGENT="이름 이메일주소" 를 넣으세요 (SEC 요청 규정)')
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self.cache = Path(cache)
        self.http = session or requests.Session()
        self.max_age = max_age_days * 86400
        self._ciks = None

    def _json(self, url, path):
        path = Path(path)
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age:
            return json.loads(path.read_text())
        time.sleep(0.15)  # SEC 권장: 초당 10회 이하
        res = self.http.get(url, headers=self.headers, timeout=30)
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise RuntimeError(f"SEC {res.status_code}: {url}")
        data = res.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return data

    def cik(self, sym):
        if self._ciks is None:
            raw = self._json(TICKERS_URL, self.cache / "company_tickers.json") or {}
            self._ciks = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in raw.values()}
        return self._ciks.get(sym.upper().replace(".", "-"))

    def facts(self, sym):
        cik = self.cik(sym)
        if cik is None:
            return None  # ETF 등 SEC 재무제표 없는 종목
        return self._json(FACTS_URL.format(cik=cik), self.cache / f"{sym.upper()}.json")


# ─── 재무 수치 정리 ───────────────────────────────────
def _d(s):
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def _ymd(s):
    return s.replace("-", "")


def entries(facts, tags, unit="USD"):
    """여러 항목 이름의 수치를 합침. 같은 기간(start,end)은 앞쪽 항목 우선"""
    gaap = (facts or {}).get("facts", {}).get("us-gaap", {})
    seen, out = set(), []
    for tag in tags:
        for e in gaap.get(tag, {}).get("units", {}).get(unit, []):
            key = (e.get("start"), e["end"])
            if key in seen or "filed" not in e:
                continue
            seen.add(key)
            out.append(e)
    return out


def quarterly(items):
    """기간 수치 → 분기별 [(분기말 YYYYMMDD, 값, 제출일 YYYYMMDD)]
    10-K 는 연간 값만 있어 4분기 = 연간 − (1~3분기). 같은 분기가 여러 번 나오면 처음 제출된 값(당시 공개된 값)"""
    q, annual = {}, []
    for e in items:
        if not e.get("start"):
            continue
        days = (_d(e["end"]) - _d(e["start"])).days
        end, filed = _ymd(e["end"]), _ymd(e["filed"])
        if 80 <= days <= 100:
            if end not in q or filed < q[end][1]:
                q[end] = (float(e["val"]), filed)
        elif 350 <= days <= 380:
            annual.append((_ymd(e["start"]), end, float(e["val"]), filed))
    for start, end, val, filed in annual:
        if end in q:
            continue
        inside = [k for k in q if start < k < end]
        if len(inside) == 3:
            q[end] = (val - sum(q[k][0] for k in inside), max([filed] + [q[k][1] for k in inside]))
    return sorted((k, v, f) for k, (v, f) in q.items())


def ttm(qs, asof, skip=0):
    """asof(YYYYMMDD)까지 제출된 분기 중 최근(skip개 건너뛴) 4개 분기 합. 연속이 아니면 None"""
    known = [x for x in qs if x[2] <= asof]
    if len(known) < 4 + skip:
        return None
    four = known[len(known) - 4 - skip:len(known) - skip]
    if (_d8(four[-1][0]) - _d8(four[0][0])).days > 300:  # 분기가 빠져 있음
        return None
    return sum(x[1] for x in four)


def _d8(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def latest(items, asof):
    """시점 값(자기자본 등) 중 asof 까지 제출된 가장 최근 값"""
    known = [(_ymd(e["end"]), _ymd(e["filed"]), float(e["val"])) for e in items
             if not e.get("start") and _ymd(e["filed"]) <= asof]
    return max(known)[2] if known else None


class Fundamentals:
    """종목별 분기 수치를 미리 정리해 두고, 날짜별 지표를 계산"""

    def __init__(self, facts):
        self.rev = quarterly(entries(facts, REVENUE_TAGS))
        self.ni = quarterly(entries(facts, NET_INCOME_TAGS))
        self.eq = entries(facts, EQUITY_TAGS)

    def growth(self, asof):
        now, prev = ttm(self.rev, asof), ttm(self.rev, asof, skip=4)
        return now / prev - 1 if now is not None and prev and prev > 0 else None

    def roe(self, asof):
        ni, eq = ttm(self.ni, asof), latest(self.eq, asof)
        return ni / eq if ni is not None and eq and eq > 0 else None
