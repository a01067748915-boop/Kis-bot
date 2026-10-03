"""
종목 목록 자동 선정 — 사람이 고르지 않고 규칙으로만 (SEC EDGAR, 무료)

  venv/bin/python make_universe.py                          # 2017년 매출 5천만 달러 이상 → 무작위 300개
  venv/bin/python make_universe.py --year 2016 --min-rev 5e8 --count 200 --seed 2

규칙
  1. SEC 에 재무제표를 낸 기업 중 해당 연도(달력 기준) 연간 매출 ≥ --min-rev
     (매출 항목 이름이 기업마다 달라 여러 항목 중 가장 큰 값)
  2. 지금 나스닥·뉴욕증권거래소·NYSE American 상장 보통주 (장외·우선주·워런트 등 제외)
  3. 조건 맞는 기업 중 --seed 로 고정한 무작위 --count 개
결과: universes/sec_auto.txt (규칙·개수·각 기업 매출이 주석으로 함께 기록)

한계: SEC 종목표는 '지금 상장된' 기업 기준이라 그 사이 상장 폐지된 기업은 빠짐 (생존 편향 일부 남음)
"""

import argparse
import os
import random
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from fundamentals import REVENUE_TAGS, Edgar

HERE = Path(__file__).resolve().parent
EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{year}.json"
EXCH = {"Nasdaq": "NAS", "NYSE": "NYS", "NYSE American": "AMS", "NYSE MKT": "AMS"}


def revenues(edgar, year):
    """{cik: (매출, 회사명)} — 매출 항목별 연간 값 중 큰 값"""
    out = {}
    for tag in REVENUE_TAGS:
        data = edgar._json(FRAMES_URL.format(tag=tag, year=year), edgar.cache / f"frames_{tag}_{year}.json") or {}
        for row in data.get("data", []):
            cik, val = int(row["cik"]), float(row["val"])
            if cik not in out or val > out[cik][0]:
                out[cik] = (val, row.get("entityName", ""))
    return out


def listed(edgar):
    """{cik: (티커, 거래소코드)} — 기업당 첫 번째(대표) 보통주 티커"""
    data = edgar._json(EXCHANGE_URL, edgar.cache / "company_tickers_exchange.json") or {}
    fields = data.get("fields", [])
    out = {}
    for row in data.get("data", []):
        r = dict(zip(fields, row))
        ex, t = EXCH.get(r.get("exchange") or ""), (r.get("ticker") or "").upper()
        if not ex or not t.isalpha() or len(t) > 5:  # 우선주(-P)·워런트(W)·유닛(U) 등 특수 표기 제외
            continue
        if len(t) == 5 and t[-1] in "WUR":  # 5글자 끝 W/U/R = 워런트·유닛·권리
            continue
        cik = int(r["cik"])
        out.setdefault(cik, (t, ex))
    return out


def build(edgar, year=2017, min_rev=5e7, count=300, seed=1):
    rev = revenues(edgar, year)
    lst = listed(edgar)
    eligible = sorted((lst[c][0], lst[c][1], v, name) for c, (v, name) in rev.items() if v >= min_rev and c in lst)
    rng = random.Random(seed)
    pick = sorted(rng.sample(eligible, min(count, len(eligible))))
    return eligible, pick


def write(path, eligible, pick, year, min_rev, seed):
    lines = [f"# 자동 선정 종목 — make_universe.py ({date.today().isoformat()} 생성)",
             f"# 규칙: SEC 재무제표 기준 {year}년 매출 ≥ {min_rev / 1e6:,.0f}백만 달러, 현재 나스닥·NYSE·NYSE American 상장 보통주",
             f"# 조건 맞는 기업 {len(eligible)}개 중 무작위 {len(pick)}개 (seed={seed}) — 사람이 고르지 않음",
             "# 한계: 지금 상장된 기업 기준이라 그 사이 상장 폐지된 기업은 빠짐", ""]
    lines += [f"{t}:{ex}   # {name[:40]} — {year} 매출 {v / 1e6:,.0f}백만 달러" for t, ex, v, name in pick]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2017)
    p.add_argument("--min-rev", type=float, default=5e7)
    p.add_argument("--count", type=int, default=300)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", default="universes/sec_auto.txt")
    a = p.parse_args()
    load_dotenv(HERE / ".env")
    edgar = Edgar(os.environ.get("SEC_USER_AGENT"))
    eligible, pick = build(edgar, a.year, a.min_rev, a.count, a.seed)
    out = HERE / a.out
    write(out, eligible, pick, a.year, a.min_rev, a.seed)
    by_ex = {}
    for _, ex, _, _ in pick:
        by_ex[ex] = by_ex.get(ex, 0) + 1
    rev, lst = revenues(edgar, a.year), listed(edgar)
    for cut in (1e7, 5e7, 1e8, 1e9):  # 기준을 바꾸면 몇 개가 되는지 참고용
        n = sum(1 for c, (v, _) in rev.items() if v >= cut and c in lst)
        print(f"  {a.year}년 매출 ≥ {cut / 1e6:,.0f}백만 달러: {n}개")
    print(f"조건 맞는 기업 {len(eligible)}개 → 무작위 {len(pick)}개 저장: {out}")
    print("거래소별: " + ", ".join(f"{k} {v}개" for k, v in sorted(by_ex.items())))
    print("다음: venv/bin/python analysis.py --only stress --targets @" + a.out + " --add none")


if __name__ == "__main__":
    main()
