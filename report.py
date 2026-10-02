"""
매매 기록 보기 — 봇이 남긴 trades.csv + 현재 보유(state.json)

  venv/bin/python report.py              # 전체 요약 + 최근 20건
  venv/bin/python report.py --days 30    # 최근 30일만
  venv/bin/python report.py --real       # 실제 주문만 (DRY_RUN 모의 기록 제외)
  venv/bin/python report.py --sym SOXX   # 한 종목만
  venv/bin/python report.py --list 100   # 최근 100건 목록
  venv/bin/python report.py --kis 30     # 증권사 계좌의 최근 30일 실제 체결내역 (봇 이전·수동 매매 포함)

텔레그램에서는 /report (최근 30일 요약)
"""

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")


def load(path=HERE / "trades.csv"):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["qty"] = int(float(r.get("qty") or 0))
        r["price"] = float(r.get("price") or 0)
        r["pnl"] = float(r["pnl"]) if r.get("pnl") not in (None, "") else None
        r["real"] = str(r.get("dry_run", "")).lower() == "false"
    return rows


def pick(rows, days=None, real=None, sym=None, today=None):
    if days:
        since = ((today or datetime.now(ET)) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["time_et"][:10] >= since]
    if real is not None:
        rows = [r for r in rows if r["real"] == real]
    if sym:
        rows = [r for r in rows if r["sym"].upper() == sym.upper()]
    return rows


def stats(rows):
    """매도(손익 확정) 기준 통계"""
    sells = [r for r in rows if r["side"] == "sell" and r["pnl"] is not None]
    n = len(sells)
    total = sum(r["pnl"] for r in sells)
    wins = [r for r in sells if r["pnl"] > 0]
    return {"buys": sum(r["side"] == "buy" for r in rows), "sells": n, "total": total,
            "win": len(wins) / n * 100 if n else 0.0, "avg": total / n if n else 0.0,
            "best": max(sells, key=lambda r: r["pnl"]) if sells else None,
            "worst": min(sells, key=lambda r: r["pnl"]) if sells else None}


def group(rows, key):
    out = {}
    for r in rows:
        if r["side"] == "sell" and r["pnl"] is not None:
            k = key(r)
            n, t, w = out.get(k, (0, 0.0, 0))
            out[k] = (n + 1, t + r["pnl"], w + (r["pnl"] > 0))
    return out


def summary_text(rows, title="매매 기록"):
    """텔레그램·터미널 공용 요약"""
    if not rows:
        return f"📒 {title}: 기록 없음"
    s = stats(rows)
    lines = [f"📒 {title} ({rows[0]['time_et'][:10]} ~ {rows[-1]['time_et'][:10]})",
             f"매수 {s['buys']}회 / 매도 {s['sells']}회, 승률 {s['win']:.0f}%",
             f"실현손익 {s['total']:+,.2f}달러 (1회 평균 {s['avg']:+,.2f})"]
    if s["best"]:
        lines.append(f"최고 {s['best']['sym']} {s['best']['pnl']:+,.2f} / 최악 {s['worst']['sym']} {s['worst']['pnl']:+,.2f}")
    by_sym = sorted(group(rows, lambda r: r["sym"]).items(), key=lambda x: -x[1][1])
    if by_sym:
        lines.append("종목별: " + ", ".join(f"{k} {t:+,.0f}({n}회)" for k, (n, t, _) in by_sym))
    return "\n".join(lines)


def holdings_text(state_file=HERE / "state.json"):
    try:
        st = json.loads(Path(state_file).read_text())
    except (OSError, ValueError):
        return "현재 보유: 확인 불가 (state.json 없음)"
    pos = st.get("positions") or {}
    if not pos:
        return "현재 보유: 없음"
    return "현재 보유: " + ", ".join(
        f"{s} {p['qty']}주 @ ${p['entry']:,.2f}" + (f" ({p['days']}일째)" if "days" in p else "") for s, p in pos.items())


def print_report(rows, list_n):
    for label, real in (("실제 주문", True), ("모의 (DRY_RUN, 주문 없음)", False)):
        part = [r for r in rows if r["real"] == real]
        if part:
            print("\n" + summary_text(part, label))
            months = group(part, lambda r: r["time_et"][:7])
            if months:
                print("월별: " + ", ".join(f"{m} {t:+,.0f}({n}회, 승률 {w / n * 100:.0f}%)"
                                         for m, (n, t, w) in sorted(months.items())))
    if not rows:
        print("\n📒 기록 없음 — 봇이 매매하면 trades.csv 에 쌓입니다.")
    print("\n" + holdings_text())
    if rows and list_n:
        print(f"\n최근 {min(list_n, len(rows))}건 (뉴욕 시간)")
        for r in rows[-list_n:]:
            pnl = f" 손익 {r['pnl']:+,.2f}" if r["pnl"] is not None else ""
            tag = "" if r["real"] else " [모의]"
            side = "매수" if r["side"] == "buy" else "매도"
            print(f"  {r['time_et'][:16]} {side} {r['sym']:<5} {r['qty']:>3}주 @ ${r['price']:,.2f}{pnl}"
                  f"  {r.get('reason', '')}{tag}")


def print_kis(days):
    """증권사 계좌 체결내역 (봇 기록과 별개, 실제 체결만)"""
    import os

    from dotenv import load_dotenv

    from kis_api import KIS, parse_targets
    load_dotenv(HERE / ".env")
    g = os.environ.get
    api = KIS(g("KIS_ENV", "mock"), g("KIS_APP_KEY"), g("KIS_APP_SECRET"), g("KIS_ACCOUNT"), HERE)
    end = datetime.now(ET)
    start = end - timedelta(days=days)
    exchanges = sorted(set(parse_targets(g("TARGETS", "QQQM:NAS")).values()))
    print(f"\n🏦 증권사 체결내역 {start:%Y-%m-%d} ~ {end:%Y-%m-%d} ({g('KIS_ENV', 'mock')})")
    seen = set()
    for ex in exchanges:
        try:
            fills = api.fills(ex, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        except Exception as e:
            print(f"  ❌ {ex} 조회 실패: {e}")
            continue
        for f in fills:
            if f["qty"] <= 0 or f["odno"] in seen:
                continue
            seen.add(f["odno"])
            side = "매수" if f["side"] == "buy" else "매도"
            print(f"  {f.get('date', '')} {side} {f['sym']:<5} {f['qty']:>3}주 @ ${f['price']:,.2f} (주문 {f['odno']})")
    if not seen:
        print("  체결 없음")
    print("  ※ 한 번에 최근 일부만 조회될 수 있습니다. 전체는 한투 앱 > 해외주식 > 체결내역")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, help="최근 N일만")
    p.add_argument("--real", action="store_true", help="실제 주문만")
    p.add_argument("--sym", help="한 종목만")
    p.add_argument("--list", type=int, default=20, help="목록 건수 (0=목록 생략)")
    p.add_argument("--kis", type=int, metavar="N", help="증권사 계좌의 최근 N일 체결내역도 조회")
    a = p.parse_args()
    rows = pick(load(), a.days, True if a.real else None, a.sym)
    print_report(rows, a.list)
    if a.kis:
        print_kis(a.kis)


if __name__ == "__main__":
    main()
