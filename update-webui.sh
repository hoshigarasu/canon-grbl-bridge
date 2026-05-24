#!/bin/bash
set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}=== $* ===${NC}"; }

LCNC_SUITE_DIR="/home/arduino/lcnc-suite"
BRIDGE_DIR="/home/arduino/canon-grbl-bridge"
DIST_DST="${BRIDGE_DIR}/lcnc-webui/dist"

[[ -d "${LCNC_SUITE_DIR}/.git" ]] || error "lcnc-suite が見つかりません: ${LCNC_SUITE_DIR}"
[[ -d "${BRIDGE_DIR}/.git" ]]     || error "canon-grbl-bridge が見つかりません: ${BRIDGE_DIR}"

step "lcnc-suite を最新に更新"
git -C "${LCNC_SUITE_DIR}" pull
COMMIT=$(git -C "${LCNC_SUITE_DIR}" rev-parse --short HEAD)
info "lcnc-suite: ${COMMIT}"

step "ThreeViewer パッチ適用"
if grep -q 'currentPosDot' "${LCNC_SUITE_DIR}/lcnc-webui/src/ThreeViewer.vue"; then
    info "ThreeViewer パッチ適用済み"
else
    node "${BRIDGE_DIR}/patches/patch_threeviewer.mjs" \
        "${LCNC_SUITE_DIR}/lcnc-webui/src/ThreeViewer.vue" && \
        info "ThreeViewer パッチ適用完了" || error "ThreeViewer パッチ失敗"
fi

step "lcnc-webui ビルド"
cd "${LCNC_SUITE_DIR}/lcnc-webui"
npm install
npm run build

step "dist を canon-grbl-bridge に反映"
rm -rf "${DIST_DST}"
mkdir -p "$(dirname "${DIST_DST}")"
cp -r "${LCNC_SUITE_DIR}/lcnc-webui/dist" "${DIST_DST}"

step "editor-widget.js 注入"
INDEX="${DIST_DST}/index.html"
if grep -q 'editor-widget' "${INDEX}"; then
    info "editor-widget.js 注入済み"
else
    sed -i 's|</body>|<script src="/editor-widget.js" defer></script>
  </body>|' "${INDEX}"
    info "editor-widget.js 注入完了"
fi

step "コミット＆プッシュ"
cd "${BRIDGE_DIR}"
if git diff --quiet HEAD -- lcnc-webui/dist; then
    info "dist に変更なし。コミットをスキップ"
else
    git add lcnc-webui/dist
    git commit -m "chore: update lcnc-webui dist (lcnc-suite ${COMMIT})"
    git push
fi

step "grbl-lcnc-gateway 再起動"
sudo systemctl restart grbl-lcnc-gateway
sleep 2
systemctl is-active --quiet grbl-lcnc-gateway && info "サービス起動中" || echo -e "${RED}[WARN]${NC}  起動失敗"
echo -e "\n${GREEN}=== 完了 ===${NC}"
