"""일봉 데이터 점검 — 분할(액면분할) 전후 가격이 어긋나는지, 예전 방식과 보정 방식을 나란히 비교

  venv/bin/python datacheck.py                       # NVDA, AMZN, GOOGL, TSLA, CMG (분할 이력 있는 종목)
  venv/bin/python datacheck.py --targets AAPL:NAS --years 10
"""

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from kis_api import KIS, parse_targets
from strategies import warn_jumps

HERE = Path(__file__).resolve().parent


def raw_history(api, sym, ex, since):
    """예전 방식: 100개씩 이어 붙이기만 함(보정 없음)"""
    bars, base = {}, ""
    while True:
        chunk = api.daily_bars(sym, ex, base)
        new = [b for b in chunk if b["date"] not in bars]
        if not new:
            break
        for b in new:
            bars[b["date"]] = b
        if chunk[0]["date"] <= since:
            break
        base = (datetime.strptime(chunk[0]["date"], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return [bars[d] for d in sorted(bars) if d >= since]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--targets", default="NVDA:NAS,AMZN:NAS,GOOGL:NAS,TSLA:NAS,CMG:NYS")
    p.add_argument("--years", type=int, default=10)
    a = p.parse_args()
    load_dotenv(HERE / ".env")
    g = os.environ.get
    api = KIS(g("KIS_ENV", "mock"), g("KIS_APP_KEY"), g("KIS_APP_SECRET"), g("KIS_ACCOUNT"), HERE)
    since = (datetime.now() - timedelta(days=365 * a.years)).strftime("%Y%m%d")
    for sym, ex in parse_targets(a.targets).items():
        raw = raw_history(api, sym, ex, since)
        fixed = api.daily_history(sym, ex, since=since)
        print(f"\n■ {sym}: {len(raw)}일, 가장 오래된 종가 예전 {raw[0]['close']:.2f} / 보정 {fixed[0]['close']:.2f}"
              if raw and fixed else f"\n■ {sym}: 데이터 없음")
        print("  예전 방식:", end="")
        if not warn_jumps(sym, raw):
            print(" 급변 없음")
        print("  보정 방식:", end="")
        if not warn_jumps(sym, fixed):
            print(" 급변 없음")
    print("\n※ 예전 방식에만 -90% 같은 급변이 있으면 분할 미반영 문제였고, 보정 방식으로 해결된 것입니다.")


if __name__ == "__main__":
    main()
