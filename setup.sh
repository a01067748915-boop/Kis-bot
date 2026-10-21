#!/bin/bash
# 서버 설치 스크립트: bash setup.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"

echo "▶ 시간대를 한국시간으로 설정"
sudo timedatectl set-timezone Asia/Seoul

echo "▶ 파이썬 설치"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

echo "▶ 가상환경 & 패키지"
python3 -m venv venv
./venv/bin/pip install -q -U pip
./venv/bin/pip install -q -U -r requirements.txt  # -U: 보안 수정된 최신 버전으로

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▶ .env 파일을 만들었습니다. 'nano .env'로 키를 입력하세요."
fi
chmod 600 .env
chmod 600 .token_*.json state.json trades.csv bot.log* .env.bak* 2>/dev/null || true
chmod 700 "${DIR}"

echo "▶ 자동 실행 서비스 등록"
sudo tee /etc/systemd/system/kisbot.service > /dev/null <<EOF
[Unit]
Description=KIS US daytrade bot
After=network-online.target
Wants=network-online.target

[Service]
User=${USER_NAME}
WorkingDirectory=${DIR}
ExecStart=${DIR}/venv/bin/python ${DIR}/bot.py
Restart=always
RestartSec=10
# 보안: 봇 폴더 외에는 쓰기 금지, 권한 상승·다른 사용자 파일 접근 차단
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${DIR}
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload

echo ""
echo "✅ 설치 완료. 다음 순서:"
echo "  1) nano .env            # 키·텔레그램 입력 후 Ctrl+O, Enter, Ctrl+X"
echo "  2) venv/bin/python check.py        # 연결 점검"
echo "  3) venv/bin/python backtest.py --sweep   # 백테스트"
echo "  4) sudo systemctl enable --now kisbot    # 봇 시작(서버 재부팅 시 자동 시작)"
echo "  로그 보기: tail -f bot.log   /  정지: sudo systemctl stop kisbot"
