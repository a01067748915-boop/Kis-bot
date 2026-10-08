#!/usr/bin/env bash
# Gate.io 봇 설치·업데이트·보안 점검 한 번에:  bash install_gatebot.sh
#  1) 최신 코드 받기 (GitHub) → 문법 검사 → 기존 파일 백업 후 교체
#  2) 예전 ~/gatebot 의 .env·기록을 /opt/gatebot 으로 옮기고 ~/gatebot 은 바로가기로 (명령어는 그대로 사용)
#  3) 전용 계정(gatebot)·권한 최소화된 서비스로 실행 (root 로 돌지 않음)
#  4) 서버 보안 정리: 명령어 기록 속 키 삭제, 한투 봇 흔적 삭제, fail2ban·자동 보안 업데이트
#  5) API 키 연결 확인 + 보안 상태 요약
set -euo pipefail
BRANCH="${BRANCH:-claude/implementable-features-sq6aav}"
BASE="https://raw.githubusercontent.com/a01067748915-boop/Kis-bot/${BRANCH}/gatebot"
APP=/opt/gatebot
OLD="${HOME}/gatebot"
SVC=gatebot
UNIT=/etc/systemd/system/${SVC}.service
FILES="bot.py gate_backtest.py README.md .env.example"
TS="$(date +%Y%m%d-%H%M%S)"
SUDO=""; RUNAS="runuser -u gatebot --"
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; RUNAS="sudo -u gatebot"; fi

echo "== Gate.io 봇 설치·보안 업데이트 =="

# ─── 1. 패키지 ───────────────────────────────────────
if command -v apt-get >/dev/null; then
  $SUDO apt-get update -qq >/dev/null && $SUDO apt-get install -y -qq python3 python3-requests curl >/dev/null
fi
python3 -c "import requests" 2>/dev/null || $SUDO python3 -m pip install -q requests --break-system-packages

# ─── 2. 최신 코드 받기 (전부 받고 검사한 뒤에 교체) ─────
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
for f in $FILES; do
  curl -fsSL "$BASE/$f" -o "$TMP/$f" || { echo "❌ $f 다운로드 실패 — 아무것도 바꾸지 않았습니다"; exit 1; }
done
python3 -m py_compile "$TMP/bot.py" "$TMP/gate_backtest.py" || { echo "❌ 받은 코드가 손상됨"; exit 1; }

# ─── 3. 전용 계정 + 폴더 ──────────────────────────────
id "$SVC" >/dev/null 2>&1 || $SUDO useradd --system --home-dir "$APP" --shell /usr/sbin/nologin "$SVC"
$SUDO mkdir -p "$APP/backup/$TS"
$SUDO systemctl stop "$SVC" 2>/dev/null || true

# 예전 위치(~/gatebot)에서 옮기기 — .env·상태·거래기록
if [ -d "$OLD" ] && [ ! -L "$OLD" ]; then
  echo "▶ 예전 폴더 ${OLD} → ${APP} 로 옮깁니다"
  for f in .env state.json trades.jsonl; do
    if [ -f "$OLD/$f" ] && [ ! -f "$APP/$f" ]; then $SUDO cp -p "$OLD/$f" "$APP/$f"; fi
  done
  $SUDO cp -rp "$OLD" "$APP/backup/$TS/old-home-gatebot"
  rm -rf "$OLD"
fi
[ -L "$OLD" ] || ln -s "$APP" "$OLD"   # 예전 명령(nano ~/gatebot/.env 등)도 그대로 동작

for f in $FILES; do
  if [ -f "$APP/$f" ]; then $SUDO cp -p "$APP/$f" "$APP/backup/$TS/"; fi
  $SUDO cp "$TMP/$f" "$APP/$f"
done
$SUDO rm -f "$APP/gatebot.service" "$APP/install_gatebot.sh"

# ─── 4. 설정(.env) ────────────────────────────────────
if [ ! -f "$APP/.env" ]; then
  $SUDO cp "$APP/.env.example" "$APP/.env"
  echo
  read -rp "Gate.io API Key: " K
  read -rsp "Gate.io API Secret (입력해도 안 보임): " SEC; echo
  read -rp "텔레그램 봇 토큰 (없으면 Enter): " TT
  TC=""; [ -n "$TT" ] && read -rp "텔레그램 chat id: " TC
  # 값은 환경변수로 넘겨 파이썬이 씀 (명령줄·sed 에 키가 드러나지 않게)
  export K SEC TT TC
  PYCODE=$(cat <<'PY'
import os, sys
p = sys.argv[1]
vals = {"GATE_KEY": os.environ["K"], "GATE_SECRET": os.environ["SEC"],
        "TELEGRAM_TOKEN": os.environ["TT"], "TELEGRAM_CHAT_ID": os.environ["TC"]}
lines = open(p, encoding="utf-8").read().splitlines()
out = [f"{l.split('=', 1)[0]}={vals[l.split('=', 1)[0]]}" if l.split("=", 1)[0] in vals else l for l in lines]
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
)
  $SUDO ${SUDO:+--preserve-env=K,SEC,TT,TC} python3 -c "$PYCODE" "$APP/.env"
  unset K SEC TT TC
else
  # 새 버전에 생긴 설정은 기본값으로 추가, 위험한 예전 기본값은 안전한 값으로
  added=""
  while IFS= read -r line; do
    k="${line%%=*}"
    case "$line" in ''|\#*) continue ;; esac
    if ! $SUDO grep -q "^${k}=" "$APP/.env"; then
      echo "$line" | $SUDO tee -a "$APP/.env" >/dev/null; added="$added ${k}"
    fi
  done < "$APP/.env.example"
  [ -n "$added" ] && echo "▶ 새 설정 추가:${added}"
  if $SUDO grep -qE '^DAILY_LOSS_LIMIT_USDT=30([^0-9.]|$)' "$APP/.env"; then
    $SUDO sed -i -E 's/^DAILY_LOSS_LIMIT_USDT=30([^0-9.].*)?$/DAILY_LOSS_LIMIT_USDT=10/' "$APP/.env"
    echo "▶ 하루 손실 한도 30 → 10 USDT (잔고 대비 너무 커서). 바꾸려면 nano ~/gatebot/.env"
  fi
  if $SUDO grep -qE '^LOOP_SEC=60([^0-9]|$)' "$APP/.env"; then
    $SUDO sed -i -E 's/^LOOP_SEC=60([^0-9].*)?$/LOOP_SEC=20/' "$APP/.env"
    echo "▶ 손절 확인 간격 60 → 20초"
  fi
fi

# ─── 5. 권한 ─────────────────────────────────────────
$SUDO chown -R "$SVC:$SVC" "$APP"
$SUDO chown -R root:root "$APP/backup"; $SUDO chmod 700 "$APP/backup"
$SUDO chmod 750 "$APP"
$SUDO chmod 600 "$APP/.env"
for f in state.json trades.jsonl; do [ -f "$APP/$f" ] && $SUDO chmod 600 "$APP/$f"; done

# ─── 6. 서비스 (전용 계정 + 권한 최소화) ──────────────
$SUDO tee "$UNIT" >/dev/null <<SVCEOF
[Unit]
Description=Gate.io trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=${SVC}
Group=${SVC}
WorkingDirectory=${APP}
ExecStart=/usr/bin/python3 ${APP}/bot.py
Restart=always
RestartSec=15
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=${APP}
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
CapabilityBoundingSet=
MemoryMax=300M

[Install]
WantedBy=multi-user.target
SVCEOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SVC" >/dev/null 2>&1
$SUDO systemctl restart "$SVC"
echo "▶ 15초간 정상 동작 확인…"
sleep 15
if systemctl is-active --quiet "$SVC" && [ "$(systemctl show -p NRestarts --value "$SVC")" = "0" ]; then
  echo "✅ 봇 정상 실행 중 (계정: ${SVC}, 폴더: ${APP})"
else
  echo "❌ 봇이 정상 실행되지 않습니다. 최근 로그:"
  $SUDO journalctl -u "$SVC" -n 20 --no-pager || true
  echo "   이전 파일 백업: ${APP}/backup/${TS}"
fi

# ─── 7. 서버 보안 정리 ───────────────────────────────
echo
echo "━━ 서버 보안 정리 ━━"
H="${HOME}/.bash_history"
if [ -f "$H" ]; then
  N=$(grep -ciE 'KEY|SECRET|TOKEN|ALPACA' "$H" || true)
  sed -i -E '/KEY|SECRET|TOKEN|ALPACA/Id' "$H"
  echo "  명령어 기록에서 키·토큰이 들어간 줄 ${N}개 삭제"
fi
if [ -e "${HOME}/kis-bot" ] || [ -f /etc/systemd/system/kisbot.service ]; then
  $SUDO systemctl disable --now kisbot 2>/dev/null || true
  $SUDO rm -rf "${HOME}/kis-bot" /etc/systemd/system/kisbot.service
  $SUDO systemctl daemon-reload
  echo "  남아 있던 한투 봇 폴더·서비스 삭제"
fi
if command -v apt-get >/dev/null; then
  if ! systemctl is-active --quiet fail2ban 2>/dev/null; then
    $SUDO apt-get install -y -qq fail2ban python3-systemd >/dev/null
    printf '[sshd]\nenabled = true\nbackend = systemd\nmaxretry = 5\nfindtime = 10m\nbantime = 1h\n' \
      | $SUDO tee /etc/fail2ban/jail.d/gatebot-sshd.local >/dev/null
    $SUDO systemctl enable --now fail2ban >/dev/null 2>&1 || true
  fi
  if ! grep -q 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null; then
    $SUDO apt-get install -y -qq unattended-upgrades >/dev/null
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
      | $SUDO tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
  fi
fi

# ─── 8. API 키 연결 확인 ─────────────────────────────
echo
echo "━━ API 키 확인 ━━"
cd "$APP"
$RUNAS python3 -c '
import bot
g = bot.Gate(bot.CFG["key"], bot.CFG["secret"])
try:
    print("  ✅ 키 연결 정상 — 현물 USDT %.2f" % g.spot_balance("USDT"))
except Exception as e:
    print("  ❌ 키 확인 실패:", bot.redact(e)[:200])
    print("     → 키 오타, 또는 Gate.io API 설정의 IP 화이트리스트에 아래 서버 IP 가 없는 경우")
' || echo "  (키 확인을 실행하지 못함)"
cd - >/dev/null

# ─── 9. 요약 ─────────────────────────────────────────
echo
echo "━━ 보안 상태 요약 ━━"
echo "  봇 실행 계정      : $(systemctl show -p User --value "$SVC" 2>/dev/null) (root 아님이어야 함)"
echo "  .env 권한         : $($SUDO stat -c '%a %U' "$APP/.env")"
echo "  fail2ban          : $(systemctl is-active fail2ban 2>/dev/null || true)"
echo "  자동 보안 업데이트 : $(grep -q 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null && echo 켜짐 || echo 꺼짐)"
echo "  SSH 비밀번호 로그인: $( ($SUDO sshd -T 2>/dev/null || true) | awk '$1=="passwordauthentication"{print ($2=="no"?"차단됨":"허용 (SSH 키로 바꾸면 더 안전)")}' | grep . || echo 확인 불가)"
echo "  모드              : $($SUDO grep -E '^DRY_RUN=' "$APP/.env" | sed 's/DRY_RUN=true/모의(주문 안 나감)/; s/DRY_RUN=false/⚠️ 실거래/')"
echo "  서버 공인 IP       : $(curl -s -m 5 ifconfig.me || echo 확인 실패)  ← Gate.io API 키의 IP 화이트리스트에 이 IP만"
echo
echo "로그        : journalctl -u ${SVC} -f"
echo "설정 바꾸기 : nano ~/gatebot/.env  →  sudo systemctl restart ${SVC}"
echo "백테스트    : cd ${APP} && sudo -u ${SVC} python3 gate_backtest.py --grid"
echo "리포트      : cd ${APP} && sudo -u ${SVC} python3 bot.py report"
