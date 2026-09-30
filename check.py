"""연결 점검: venv/bin/python check.py"""

import math
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from bot import ET, Config, Telegram, usd
from kis_api import KIS

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
cfg = Config()
print(f"환경: {cfg.env} / DRY_RUN: {cfg.dry_run} / 예산 {usd(cfg.budget)} / 종목당 {usd(cfg.alloc)}")
print(f"뉴욕 현재시각: {datetime.now(ET):%Y-%m-%d %H:%M}")

api = KIS(cfg.env, os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"], os.environ["KIS_ACCOUNT"], HERE)
for sym, ex in cfg.targets.items():
    try:
        p = api.price(sym, ex)
        bars = api.daily_bars(sym, ex)
        qty = math.floor(cfg.alloc / (p * (1 + cfg.limit_slip_pct / 100)))
        last = bars[-1]["date"] if bars else "없음"
        warn = "" if qty > 0 else "  ⚠️ 배정액으로 1주도 못 삼 → 종목 변경 필요"
        print(f"✅ {sym}({ex}) 현재가 {usd(p)}, 일봉 {len(bars)}개(최근 {last}), 매수가능 {qty}주{warn}")
    except Exception as e:
        print(f"❌ {sym}({ex}) 실패: {e}")

for ex in sorted(set(cfg.targets.values())):
    try:
        h = api.holdings(ex)
        print(f"✅ 잔고 조회({ex}) 성공: {len(h)}종목")
        for sym, v in h.items():
            print(f"   {v['name']}({sym}) {v['qty']}주 @ {usd(v['avg'])}")
    except Exception as e:
        print(f"❌ 잔고 조회({ex}) 실패: {e}")

tg = Telegram(cfg.tg_token, cfg.tg_chat)
if tg.enabled:
    tg.send("✅ 텔레그램 연결 테스트 성공")
    print("✅ 텔레그램 메시지 보냄 — 폰에서 확인하세요")
else:
    print("⚠️ 텔레그램 설정이 비어 있음")
