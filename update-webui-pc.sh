#!/bin/bash
# PC (Git Bash) から実行する lcnc-webui ビルド & 転送スクリプト
# UNO Qよりも約10倍速くビルドできる
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}=== $* ===${NC}"; }

LCNC_DIR="$HOME/Documents/cnc/lcnc-suite"
BRIDGE_DIR="$HOME/Documents/cnc/canon-grbl-bridge"
UNO_Q="uno-q"
REMOTE_BRIDGE="/home/arduino/canon-grbl-bridge"

# node が PATH にあるか確認（なければ npm の隣を探す）
if ! command -v node &>/dev/null; then
    NPM_DIR=$(dirname "$(command -v npm 2>/dev/null || echo '')")
    [[ -n "$NPM_DIR" && -f "$NPM_DIR/node" ]] && export PATH="$NPM_DIR:$PATH" \
        || error "node が見つかりません。Node.js をインストールしてください"
fi

[[ -d "$LCNC_DIR/.git" ]]   || error "lcnc-suite が見つかりません: $LCNC_DIR"
[[ -d "$BRIDGE_DIR/.git" ]] || error "canon-grbl-bridge が見つかりません: $BRIDGE_DIR"

step "lcnc-suite を最新に更新"
git -C "$LCNC_DIR" pull
COMMIT=$(git -C "$LCNC_DIR" rev-parse --short HEAD)
info "lcnc-suite: $COMMIT"

step "ThreeViewer パッチ適用"
VUE="$LCNC_DIR/lcnc-webui/src/ThreeViewer.vue"
MJS="$BRIDGE_DIR/patches/patch_threeviewer.mjs"
if grep -q 'currentPosDot' "$VUE"; then
    info "ThreeViewer パッチ適用済み"
elif [[ -f "$MJS" ]]; then
    node "$MJS" "$VUE" && info "ThreeViewer パッチ適用完了" \
        || error "ThreeViewer パッチ失敗"
else
    error "パッチスクリプトが見つかりません: $MJS"
fi

step "lcnc-webui ビルド (PC)"
cd "$LCNC_DIR/lcnc-webui"
npm install --silent
npm run build
info "ビルド完了"

step "dist を UNO Q に転送"
ssh "$UNO_Q" "rm -rf $REMOTE_BRIDGE/lcnc-webui/dist"
scp -r dist/ "$UNO_Q:$REMOTE_BRIDGE/lcnc-webui/dist"
info "転送完了"

step "editor-widget.js 注入"
ssh "$UNO_Q" "
INDEX='$REMOTE_BRIDGE/lcnc-webui/dist/index.html'
if grep -q 'editor-widget' \"\$INDEX\"; then
    echo '[INFO]  editor-widget.js 注入済み'
else
    sed -i 's|</body>|<script src=\"/editor-widget.js\" defer></script>\n  </body>|' \"\$INDEX\"
    echo '[INFO]  editor-widget.js 注入完了'
fi
"

step "コミット & プッシュ"
cd "$BRIDGE_DIR"
git pull --quiet
git add lcnc-webui/dist
if git diff --quiet --cached; then
    info "dist に変更なし。コミットをスキップ"
else
    git commit -m "chore: update lcnc-webui dist (lcnc-suite $COMMIT)"
    git push
    info "プッシュ完了"
fi

step "grbl-lcnc-gateway 再起動"
ssh -t "$UNO_Q" 'sudo systemctl restart grbl-lcnc-gateway && sleep 2 && systemctl is-active grbl-lcnc-gateway'

echo -e "\n${GREEN}=== 完了 ===${NC}"
