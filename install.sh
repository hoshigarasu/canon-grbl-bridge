#!/bin/bash
# =============================================================================
# Arduino UNO Q CNC Controller インストーラ
# 対象: Arduino UNO Q (QRB2210 / Debian 13 trixie / aarch64)
# リポジトリ: https://github.com/hoshigarasu/canon-grbl-bridge
# =============================================================================
set -e

# --- カラー出力 ---------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}=== $* ===${NC}"; }

# --- 定数 ---------------------------------------------------------------------
REPO_URL="https://github.com/hoshigarasu/canon-grbl-bridge.git"
INSTALL_DIR="/home/arduino/canon-grbl-bridge"
NGC_DIR="/home/arduino/ngc"
SETTINGS_DIR="/home/arduino/.config/lcnc_gateway"
SERVICE_NAME="grbl-lcnc-gateway"

# --- 前提確認 -----------------------------------------------------------------
step "環境確認"

[[ "$(uname -m)" == "aarch64" ]] || error "aarch64 以外では動作しません"
[[ "$(id -un)" == "arduino" ]]   || error "arduino ユーザーで実行してください"

DEBIAN_VER=$(grep VERSION_CODENAME /etc/os-release | cut -d= -f2)
info "Debian: ${DEBIAN_VER}"
info "ユーザー: $(id -un)"

# --- パッケージ更新 -----------------------------------------------------------
step "システムパッケージ更新"
sudo apt-get update -q

# --- linuxcnc-uspace (rs274ngc) -----------------------------------------------
step "linuxcnc-uspace インストール確認"
if ! dpkg -s linuxcnc-uspace &>/dev/null; then
    info "linuxcnc-uspace を検索中..."
    if apt-cache show linuxcnc-uspace &>/dev/null; then
        sudo apt-get install -y linuxcnc-uspace
    else
        # LinuxCNC 公式リポジトリを追加
        warn "標準リポジトリに見つかりません。LinuxCNC リポジトリを追加します"
        sudo apt-get install -y curl gpg
        curl -fsSL https://linuxcnc.org/linuxcnc.gpg | sudo gpg --dearmor -o /usr/share/keyrings/linuxcnc.gpg
        echo "deb [signed-by=/usr/share/keyrings/linuxcnc.gpg] http://linuxcnc.org/ ${DEBIAN_VER} base" \
            | sudo tee /etc/apt/sources.list.d/linuxcnc.list
        sudo apt-get update -q
        sudo apt-get install -y linuxcnc-uspace
    fi
else
    info "linuxcnc-uspace: インストール済み ($(dpkg -s linuxcnc-uspace | grep Version | awk '{print $2}'))"
fi

# --- 依存パッケージ -----------------------------------------------------------
step "依存パッケージインストール"
sudo apt-get install -y \
    git \
    python3 \
    python3-pip \
    python3-gpiod \
    nodejs \
    npm

info "Node.js: $(node -v)"
info "npm:     $(npm -v)"
info "Python:  $(python3 --version)"

# --- Python パッケージ --------------------------------------------------------
step "Python パッケージインストール"
pip3 install --break-system-packages \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    gpiod

# --- リポジトリ ---------------------------------------------------------------
step "リポジトリセットアップ"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "既存リポジトリを更新: ${INSTALL_DIR}"
    git -C "${INSTALL_DIR}" pull
else
    info "クローン: ${REPO_URL}"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

# --- ディレクトリ作成 ---------------------------------------------------------
step "データディレクトリ作成"
mkdir -p "${NGC_DIR}"
mkdir -p "${SETTINGS_DIR}"
info "NGC 保存先:     ${NGC_DIR}"
info "設定保存先:     ${SETTINGS_DIR}"

# --- systemd サービス ---------------------------------------------------------
step "systemd サービス登録"
SERVICE_SRC="${INSTALL_DIR}/grbl-lcnc-gateway.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

[[ -f "${SERVICE_SRC}" ]] || error "サービスファイルが見つかりません: ${SERVICE_SRC}"

sudo cp "${SERVICE_SRC}" "${SERVICE_DST}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
info "サービス登録完了: ${SERVICE_NAME}"

# --- 起動 ---------------------------------------------------------------------
step "サービス起動"
sudo systemctl restart "${SERVICE_NAME}"
sleep 3

if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "サービス起動成功"
else
    warn "サービス起動に問題があります"
    sudo journalctl -u "${SERVICE_NAME}" -n 20 --no-pager
fi

# --- 完了 ---------------------------------------------------------------------
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  インストール完了${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  ブラウザでアクセス:"
IP=$(hostname -I | awk '{print $1}')
echo -e "  ${CYAN}http://${IP}:8000${NC}"
echo ""
echo -e "  ログ確認:"
echo -e "  sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
