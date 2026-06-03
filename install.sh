#!/bin/bash
# =============================================================================
# Arduino UNO Q CNC Controller インストーラ
# 対象: Arduino UNO Q (QRB2210 / Debian 13 trixie / aarch64)
# リポジトリ: https://github.com/hoshigarasu/canon-grbl-bridge
#
# 使い方:
#   初回（grblHAL ファーム書き込み込み）:  ./install.sh --flash-firmware
#   ソフト更新のみ（再フラッシュ不要）:    ./install.sh
# =============================================================================
set -e

# --- カラー出力 ---------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}=== $* ===${NC}"; }

# LAN IP（docker0 等の内部 IF ではなく、外向きルートの送信元アドレス）
lan_ip() {
    local ip
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
    [ -z "$ip" ] && ip=$(hostname -I | awk '{print $1}')
    echo "$ip"
}

# --- 引数解析 -----------------------------------------------------------------
FLASH_FW=0
for arg in "$@"; do
    case "$arg" in
        --flash-firmware|--flash-fw) FLASH_FW=1 ;;
        -h|--help)
            echo "使い方: $0 [--flash-firmware]"
            echo "  --flash-firmware  grblHAL ファームを STM32 に書き込む（初回必須・電源サイクル1回が必要）"
            exit 0 ;;
        *) warn "不明な引数: $arg (無視します)" ;;
    esac
done

# --- 定数 ---------------------------------------------------------------------
REPO_URL="https://github.com/hoshigarasu/canon-grbl-bridge.git"
INSTALL_DIR="/home/arduino/canon-grbl-bridge"
NGC_DIR="/home/arduino/ngc"
SETTINGS_DIR="/home/arduino/.config/lcnc_gateway"
SERVICE_NAME="grbl-lcnc-gateway"
FW="${INSTALL_DIR}/firmware/grblHAL_UNO_Q.elf"

# --- grblHAL フラッシュ関数 ---------------------------------------------------
# QRB2210 オンボード OpenOCD で STM32U585 へ SWD 書き込み。
# 注意: OpenOCD のウォームリセットでは RSS/デバッグドメインの関係で grblHAL が
#       起動しきらないため、reset run は行わず、書き込み後に物理 POR を促す。
flash_grblhal() {
    step "grblHAL ファームウェア書き込み (SWD)"
    [[ -x /opt/openocd/bin/openocd ]] \
        || error "OpenOCD が見つかりません (/opt/openocd/bin/openocd)。UNO Q 工場イメージか確認してください"
    [[ -f /opt/openocd/openocd_gpiod.cfg ]] \
        || error "OpenOCD 設定が見つかりません (/opt/openocd/openocd_gpiod.cfg)"
    [[ -f "${FW}" ]] \
        || error "ファームが見つかりません: ${FW}  (リポジトリの firmware/ にコミットされているか確認)"
    info "ファーム: ${FW} ($(stat -c%s "${FW}") bytes)"

    # ttyHS1 を解放（gateway を止める）
    sudo systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    sleep 0.5

    info "OpenOCD で書き込み中... (約30秒、抜かないでください)"
    sudo /opt/openocd/bin/openocd \
        -s /opt/openocd -s /opt/openocd/share/openocd/scripts \
        -f /opt/openocd/openocd_gpiod.cfg \
        -c "init" -c "reset halt" \
        -c "flash write_image erase ${FW}" \
        -c "verify_image ${FW}" \
        -c "shutdown"
    info "書き込み・ベリファイ完了"
}

# --- 前提確認 -----------------------------------------------------------------
step "環境確認"

[[ "$(uname -m)" == "aarch64" ]] || error "aarch64 以外では動作しません"
[[ "$(id -un)" == "arduino" ]]   || error "arduino ユーザーで実行してください"

DEBIAN_VER=$(grep VERSION_CODENAME /etc/os-release | cut -d= -f2)
info "Debian: ${DEBIAN_VER}"
info "ユーザー: $(id -un)"
[[ "${FLASH_FW}" == "1" ]] && info "モード: ソフト + grblHAL ファーム書き込み" \
                           || info "モード: ソフトのみ（--flash-firmware で初回ファーム書き込み）"

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
    gpiod \
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

# --- arduino-router 無効化 ----------------------------------------------------
# 工場イメージの arduino-router は RouterBridge プロトコルで ttyHS1 を掴み、
# grbl-lcnc-gateway の grbl プロトコルと衝突する（「正常に見えるが動かない」）。
# grblHAL 運用では必ず無効化し、ttyHS1 は gateway だけが占有する。
step "arduino-router 無効化 (ttyHS1 競合の排除)"
if systemctl list-unit-files 2>/dev/null | grep -q '^arduino-router'; then
    sudo systemctl disable --now arduino-router 2>/dev/null || true
    info "arduino-router を停止・無効化しました"
else
    info "arduino-router は存在しません（スキップ）"
fi

# --- systemd サービス ---------------------------------------------------------
step "systemd サービス登録"
SERVICE_SRC="${INSTALL_DIR}/grbl-lcnc-gateway.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

[[ -f "${SERVICE_SRC}" ]] || error "サービスファイルが見つかりません: ${SERVICE_SRC}"

sudo cp "${SERVICE_SRC}" "${SERVICE_DST}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
info "サービス登録完了: ${SERVICE_NAME}"

# --- grblHAL 書き込み（--flash-firmware 指定時のみ）---------------------------
if [[ "${FLASH_FW}" == "1" ]]; then
    flash_grblhal

    # 書き込み後は POR が必要なため gateway は起動せず、電源サイクルを案内して終了。
    # サービスは enable 済みなので、次回起動時に自動起動する。
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  ソフトウェア + ファーム書き込み 完了${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "  ${YELLOW}👉 ボードの電源を入れ直してください。${NC}"
    echo -e "     grblHAL の起動には電源サイクル1回が必要です。"
    echo -e "     再投入後、gateway は自動起動します。"
    echo ""
    IP=$(lan_ip)
    echo -e "  電源再投入後アクセス: ${CYAN}http://${IP}:8000${NC}"
    echo ""
    exit 0
fi

# --- 起動（ソフトのみモード）-------------------------------------------------
step "サービス起動"
sudo systemctl restart "${SERVICE_NAME}"
sleep 3

if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "サービス起動成功"
else
    warn "サービス起動に問題があります"
    sudo journalctl -u "${SERVICE_NAME}" -n 20 --no-pager
    warn "初回インストールの場合は --flash-firmware を付けて grblHAL を書き込んでください"
fi

# --- 完了 ---------------------------------------------------------------------
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  インストール完了${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  ブラウザでアクセス:"
IP=$(lan_ip)
echo -e "  ${CYAN}http://${IP}:8000${NC}"
echo ""
echo -e "  ログ確認:"
echo -e "  sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
