#!/bin/bash
# 보안 패치 한 번에 적용: bash secure_update.sh   (curl | bash 로 실행하지 말 것 — 질문에 답할 수 없음)
#  1) 봇 정지 → 백업 → 최신 코드 받기 → setup.sh(권한·서비스 격리) → 점검 → 재시작
#  2) 재시작 실패 시 자동으로 이전 버전 복구
#  3) 서버 보안(자동 보안 업데이트, 방화벽, SSH 비밀번호 로그인 차단)은 하나씩 물어보고 적용
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
BRANCH="${BRANCH:-claude/implementable-features-sq6aav}"
BASE="https://raw.githubusercontent.com/a01067748915-boop/Kis-bot/${BRANCH}"
FILES="bot.py kis_api.py signals.py report.py datacheck.py progress.sh minute_backtest.py news_backtest.py analysis.py fundamentals.py make_universe.py check.py backtest.py strategies.py setup.sh requirements.txt .env.example .gitignore"
UNIT=/etc/systemd/system/kisbot.service
BACKUP="${DIR}/backup/$(date +%Y%m%d-%H%M%S)"

ask() {  # ask "질문" 기본값(y/n)
  local ans
  read -r -p "$1 [$( [ "$2" = y ] && echo Y/n || echo y/N )] " ans || true
  ans="${ans:-$2}"
  [[ "$ans" =~ ^[Yyㅛ] ]]  # 한글 자판 상태의 y(ㅛ)도 예로 인식
}

echo "▶ 봇 폴더: ${DIR}"
[ -f .env ] || { echo "❌ .env 가 없습니다. 봇 폴더에서 실행하세요."; exit 1; }

# ─── 1. 정지 & 백업 ───────────────────────────────────
WAS_RUNNING=no
if systemctl is-active --quiet kisbot 2>/dev/null; then
  WAS_RUNNING=yes
  echo "▶ 봇 정지 (state.json 에 보유 현황이 남아 재시작 후 이어서 관리합니다)"
  sudo systemctl stop kisbot
fi
mkdir -p "$BACKUP"
for f in $FILES; do
  if [ -f "$f" ]; then cp -p "$f" "$BACKUP/"; fi
done
if [ -f "$UNIT" ]; then sudo cp -p "$UNIT" "$BACKUP/kisbot.service"; fi
echo "▶ 백업: ${BACKUP}"

restore() {
  echo "↩️  이전 버전으로 복구합니다"
  for f in $FILES; do
    if [ -f "$BACKUP/$f" ]; then cp -p "$BACKUP/$f" "$f"; fi
  done
  if [ -f "$BACKUP/kisbot.service" ]; then
    sudo cp -p "$BACKUP/kisbot.service" "$UNIT"
    sudo systemctl daemon-reload
  fi
  if [ "$WAS_RUNNING" = yes ]; then sudo systemctl start kisbot || true; fi
  echo "   복구 완료. 원인 확인: journalctl -u kisbot -n 50"
}

abort() {  # 파일을 바꾸기 전 실패 → 원래대로 다시 시작만
  echo "❌ $1 — 아무것도 바꾸지 않았습니다"
  if [ "$WAS_RUNNING" = yes ]; then sudo systemctl start kisbot || true; fi
  exit 1
}

# ─── 2. 최신 코드 받기 (전부 받은 뒤에 한꺼번에 교체) ─
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "▶ 최신 코드 받기 (${BRANCH})"
for f in $FILES; do
  curl -fsSL "$BASE/$f" -o "$TMP/$f" || abort "$f 다운로드 실패"
done
python3 -m py_compile "$TMP"/*.py \
  || abort "받은 코드가 손상됨"
for f in $FILES; do cp "$TMP/$f" "$f"; done
chmod +x setup.sh

# ─── 3. setup.sh: 패키지·권한·서비스 격리 ─────────────
echo "▶ setup.sh 실행"
if ! bash setup.sh; then
  echo "❌ setup.sh 실패"; restore; exit 1
fi

# ─── 4. 점검 ──────────────────────────────────────────
echo "▶ 연결 점검 (🔐 표시가 나오면 안내대로 조치하세요)"
venv/bin/python check.py || echo "⚠️ check.py 오류 — 위 내용을 확인하세요"

# ─── 5. 재시작 & 정상 동작 확인 ───────────────────────
if [ "$WAS_RUNNING" = yes ] || ask "봇을 시작할까요?" y; then
  sudo systemctl enable --now kisbot
  echo "▶ 20초간 정상 동작 확인…"
  sleep 20
  if ! systemctl is-active --quiet kisbot || [ "$(systemctl show -p NRestarts --value kisbot)" != "0" ]; then
    echo "❌ 봇이 정상적으로 실행되지 않습니다:"
    journalctl -u kisbot -n 30 --no-pager || true
    sudo systemctl stop kisbot
    restore
    exit 1
  fi
  echo "✅ 봇 정상 실행 중"
else
  echo "⏸  봇을 시작하지 않았습니다 — 켜려면: sudo systemctl enable --now kisbot"
fi

# ─── 6. 서버 보안 (선택) ─────────────────────────────
echo ""
echo "━━ 서버 보안 설정 (원하는 것만 y) ━━"

if ask "① 보안 업데이트 자동 설치(unattended-upgrades)를 켤까요?" y; then
  sudo apt-get install -y -qq unattended-upgrades
  echo 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades > /dev/null
  echo "   ✅ 자동 보안 업데이트 켜짐"
fi

SSH_PORTS="$( (sudo sshd -T 2>/dev/null || true) | awk '$1=="port"{print $2}' | sort -u | tr '\n' ' ')"
SSH_PORTS="${SSH_PORTS:-22}"
echo ""
echo "② 방화벽(ufw): SSH(포트 ${SSH_PORTS}) 외 들어오는 연결 차단. 봇은 들어오는 연결이 필요 없습니다."
echo "   ※ 오라클 클라우드는 자체 iptables 규칙과 충돌할 수 있어 n 권장"
if ask "   방화벽을 켤까요?" n; then
  sudo apt-get install -y -qq ufw
  for p in $SSH_PORTS; do sudo ufw allow "${p}/tcp" > /dev/null; done
  sudo ufw default deny incoming > /dev/null
  sudo ufw default allow outgoing > /dev/null
  sudo ufw --force enable
  echo "   ✅ 방화벽 켜짐 (허용: ${SSH_PORTS})"
fi

echo ""
echo "③ SSH 비밀번호 로그인 차단 (키로만 접속)"
KEYS="${HOME}/.ssh/authorized_keys"
if [ ! -s "$KEYS" ]; then
  echo "   ⏭  ${KEYS} 에 등록된 키가 없어 건너뜁니다 (지금 막으면 접속이 끊길 수 있음)"
elif ask "   지금 SSH 키로 접속 중이 맞나요? 비밀번호 로그인을 막을까요?" n; then
  CONF=/etc/ssh/sshd_config.d/01-kisbot-hardening.conf  # 01: 클라우드 기본 설정(50-*)보다 먼저 적용
  if ! sudo grep -qE '^\s*Include\s+/etc/ssh/sshd_config.d/' /etc/ssh/sshd_config; then
    echo "   ⏭  이 서버의 sshd 가 sshd_config.d 를 읽지 않아 건너뜁니다"
  else
    printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
      | sudo tee "$CONF" > /dev/null
    if sudo sshd -t; then
      sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
      echo "   ✅ 비밀번호 로그인 차단됨. 이 창을 닫기 전에 새 터미널에서 접속되는지 꼭 확인하세요!"
      echo "      문제 시 되돌리기: sudo rm ${CONF} && sudo systemctl reload ssh"
    else
      sudo rm -f "$CONF"
      echo "   ❌ 설정 검사 실패로 되돌렸습니다"
    fi
  fi
fi

echo ""
echo "🎉 보안 패치 완료. 로그: tail -f ${DIR}/bot.log"
echo "   백업: ${BACKUP}"
