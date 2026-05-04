#!/bin/bash
# K-Train (SRT + KTX) 매크로 통합 설치 스크립트 (macOS)
# 사용법:
#   curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-train-macro/main/install.sh | bash
set -e

REPO="https://github.com/Chihun-Lee/k-train-macro.git"
INSTALL_DIR="${K_TRAIN_HOME:-$HOME/.k-train-macro}"
APP_DIR="$HOME/Applications"
RUN_APP="$APP_DIR/기차 매크로.app"
QUIT_APP="$APP_DIR/기차 매크로 종료.app"
PORT=8912

echo ""
echo "════════════════════════════════════════"
echo "  K-Train 매크로 (SRT + KTX) 설치"
echo "════════════════════════════════════════"
echo ""

echo "[1/5] Python 확인..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "  → Python3가 없습니다. macOS 명령행 도구를 설치합니다."
  xcode-select --install 2>&1 || true
  echo "  설치가 끝나면 이 .command 파일을 다시 더블클릭하세요."
  read -p "  엔터 키를 누르면 창이 닫힙니다..."
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  ✓ Python $PY_VER"

echo "[2/5] 코드 다운로드..."
mkdir -p "$APP_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --quiet
  git -C "$INSTALL_DIR" reset --hard origin/main --quiet
else
  if [ -d "$INSTALL_DIR" ]; then rm -rf "$INSTALL_DIR"; fi
  git clone --quiet "$REPO" "$INSTALL_DIR"
fi
echo "  ✓ $INSTALL_DIR"

echo "[3/5] Python 환경 구성 (1~3분 소요)..."
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "  ✓ 의존성 설치 완료"

echo "[4/5] 앱 번들 생성..."
rm -rf "$RUN_APP" "$QUIT_APP"

mkdir -p "$RUN_APP/Contents/MacOS"
cat > "$RUN_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>기차 매크로</string>
  <key>CFBundleDisplayName</key><string>기차 매크로</string>
  <key>CFBundleIdentifier</key><string>com.chihunlee.k-train-macro</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>k-train-macro</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

cat > "$RUN_APP/Contents/MacOS/k-train-macro" <<EOF
#!/bin/bash
INSTALL_DIR="$INSTALL_DIR"
PORT=$PORT
LOG="/tmp/k-train-macro.log"

EXISTING=\$(lsof -ti tcp:\$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "\$EXISTING" ]; then
  kill \$EXISTING 2>/dev/null
  sleep 1
fi

cd "\$INSTALL_DIR"
nohup "\$INSTALL_DIR/venv/bin/python" server.py > "\$LOG" 2>&1 &

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS http://127.0.0.1:\$PORT/api/srt/config/status > /dev/null 2>&1; then
    open "http://127.0.0.1:\$PORT"
    osascript -e 'display notification "SRT + KTX 탭으로 사용하세요." with title "기차 매크로 시작됨" sound name "Glass"'
    exit 0
  fi
  sleep 1
done

osascript -e 'display alert "기차 매크로 시작 실패" message "로그: /tmp/k-train-macro.log" as critical'
EOF
chmod +x "$RUN_APP/Contents/MacOS/k-train-macro"

mkdir -p "$QUIT_APP/Contents/MacOS"
cat > "$QUIT_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>기차 매크로 종료</string>
  <key>CFBundleDisplayName</key><string>기차 매크로 종료</string>
  <key>CFBundleIdentifier</key><string>com.chihunlee.k-train-macro-quit</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>quit</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

cat > "$QUIT_APP/Contents/MacOS/quit" <<EOF
#!/bin/bash
PORT=$PORT
PIDS=\$(lsof -ti tcp:\$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "\$PIDS" ]; then
  kill \$PIDS
  osascript -e 'display notification "기차 매크로 종료됨" with title "기차 매크로" sound name "Pop"'
else
  osascript -e 'display notification "이미 종료된 상태입니다" with title "기차 매크로"'
fi
EOF
chmod +x "$QUIT_APP/Contents/MacOS/quit"

xattr -dr com.apple.quarantine "$RUN_APP" 2>/dev/null || true
xattr -dr com.apple.quarantine "$QUIT_APP" 2>/dev/null || true

echo "  ✓ $RUN_APP"
echo "  ✓ $QUIT_APP"

echo "[5/5] 완료!"
echo ""
echo "════════════════════════════════════════"
echo "  ✅ 설치 완료"
echo "════════════════════════════════════════"
echo ""
echo "  사용법:"
echo "    1. Launchpad → '기차 매크로' 검색 → 더블클릭"
echo "    2. 브라우저가 자동으로 열림 (SRT / KTX 탭)"
echo "    3. 두 탭 동시 사용 가능"
echo ""
echo "  종료: Launchpad → '기차 매크로 종료' 더블클릭"
echo ""

read -p "  지금 바로 실행할까요? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  open "$RUN_APP"
fi
