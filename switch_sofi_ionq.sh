#!/bin/bash
# 매매 종목을 SOFI·IONQ 로 바꾸고 권장 설정까지 한 번에: bash switch_sofi_ionq.sh
#  1) .env 백업  2) 종목·손절 등 권장값 적용  3) 처음 며칠은 주문 없이(DRY_RUN) 지켜보기
#  4) 시세 점검  5) 3년 백테스트(K 0.3~0.7 비교)  6) 재시작 후 정상 동작 확인
# 되돌리기: cp .env.bak-<날짜> .env && sudo systemctl restart kisbot
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "❌ .env 가 없습니다. 봇 폴더(~/kis-bot)에서 실행하세요."; exit 1; }
[ -x venv/bin/python ] || { echo "❌ venv 가 없습니다. 먼저 bash setup.sh 를 실행하세요."; exit 1; }

ask() {  # ask "질문" 기본값(y/n) — 한글 자판의 ㅛ 도 예
  local ans
  read -r -p "$1 [$( [ "$2" = y ] && echo Y/n || echo y/N )] " ans || true
  ans="${ans:-$2}"
  [[ "$ans" =~ ^[Yyㅛ] ]]
}
setenv() {  # 항목이 있으면 값만 바꾸고, 없으면 추가
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
show() {
  grep -E "^(KIS_ENV|DRY_RUN|BUDGET_USD|TARGETS|K|STOP_LOSS_PCT|MAX_CHASE_PCT|LIMIT_SLIP_PCT|DAILY_LOSS_LIMIT_PCT)=" .env \
    | sed 's/^/   /'
}

# ─── 1. 백업 ──────────────────────────────────────────
BAK=".env.bak-$(date +%Y%m%d-%H%M%S)"
cp -p .env "$BAK"
echo "▶ 기존 설정 백업: $BAK"
show

# ─── 2. 권장 설정 ─────────────────────────────────────
echo ""
echo "▶ 권장 설정 적용"
setenv TARGETS SOFI:NAS,IONQ:NYS     # 소파이(나스닥), 아이온큐(뉴욕증권거래소)
setenv STOP_LOSS_PCT 4               # 하루 4~10% 움직임이 흔해 2%면 손절이 너무 잦음
setenv MAX_CHASE_PCT 1.5             # 급등 시 진입 기회 조금 더 허용
setenv LIMIT_SLIP_PCT 0.5            # 빠르게 움직여도 지정가가 체결되게
setenv DAILY_LOSS_LIMIT_PCT 5        # 예산 대비 하루 최대 손실
grep -q "^K=" .env || setenv K 0.5   # K 는 기존 값 유지 (백테스트 보고 조정)

# ─── 3. 주문 없이 지켜보기 ────────────────────────────
echo ""
echo "▶ 권장: 종목을 바꾼 뒤 3~5거래일은 DRY_RUN=true (텔레그램 알림만, 실제 주문 없음)"
if ask "  DRY_RUN=true 로 둘까요?" y; then
  setenv DRY_RUN true
  echo "   → 주문 없음 모드. 결과가 괜찮으면 나중에: sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' .env && sudo systemctl restart kisbot"
else
  echo "   → 현재 DRY_RUN 값 유지"
fi
chmod 600 .env
echo ""
echo "▶ 적용된 설정"
show

# ─── 4. 시세 점검 ─────────────────────────────────────
echo ""
echo "▶ 시세·계좌 점검 (SOFI, IONQ 모두 ✅ 이고 매수가능 1주 이상이어야 함)"
CHECK="$(venv/bin/python check.py 2>&1 || true)"
echo "$CHECK"
if ! echo "$CHECK" | grep -q "✅ SOFI" || ! echo "$CHECK" | grep -q "✅ IONQ" \
   || echo "$CHECK" | grep -q "1주도 못 삼"; then
  echo ""
  echo "❌ 종목 점검 실패 — 설정을 되돌립니다"
  cp -p "$BAK" .env
  exit 1
fi

# ─── 5. 백테스트 ──────────────────────────────────────
echo ""
if ask "▶ 3년 백테스트(K 0.3~0.7 비교)를 돌릴까요? (1~2분)" y; then
  venv/bin/python backtest.py --sweep --years 3 || echo "⚠️ 백테스트 실패 — 봇 동작과는 무관합니다"
  echo ""
  echo "   보는 법: 누적수익(%)이 높으면서 최대낙폭(%)이 덜 깊은 K를 고르세요."
  echo "   바꾸려면: sed -i 's/^K=.*/K=0.6/' .env   (0.6 자리에 고른 값)"
fi

# ─── 6. 재시작 & 확인 ─────────────────────────────────
echo ""
echo "▶ 봇 재시작 (기존 QQQM·SOXX 보유분이 있으면 손절·장마감 매도까지 계속 관리)"
sudo systemctl restart kisbot
sleep 20
if systemctl is-active --quiet kisbot; then
  echo "✅ 봇 정상 실행 중 — 텔레그램에 시작 알림이 왔는지 확인하세요"
else
  echo "❌ 봇이 실행되지 않습니다:"
  journalctl -u kisbot -n 30 --no-pager || true
  echo "   설정을 되돌리려면: cp $BAK .env && sudo systemctl restart kisbot"
  exit 1
fi

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 앞으로 할 일 (권장)
 1. 매일 22:35(한국시간, 서머타임 기준) 이후 텔레그램 /status 로 목표가 확인
 2. DRY_RUN 으로 3~5거래일 알림을 지켜보고, 매수·손절 타이밍이 납득되면 실제 주문 전환
 3. 실적 발표일(SOFI·IONQ 각각 분기마다)은 시가가 크게 튈 수 있어 /pause 권장
    → 다음 날 /resume
 4. 일주일에 한 번 trades.csv 로 손익 확인:  column -s, -t trades.csv | tail -20
 5. 하루 손실 한도: 예산의 5% — 연속 손실이 이어지면 /pause 후 설정 재검토
 되돌리기: cp $BAK .env && sudo systemctl restart kisbot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
