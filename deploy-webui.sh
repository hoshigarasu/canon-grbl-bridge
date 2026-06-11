cat > /c/Tools/canon-grbl-bridge/deploy-webui.sh << 'EOF'
#!/usr/bin/env bash
# PC 側で実行: lcnc-webui の変更を UNO Q でビルドしてデプロイ
set -e
cd "$(dirname "$0")/../lcnc-suite"
echo "=== git push lcnc-suite ==="
git push origin main
echo "=== build + deploy on UNO Q ==="
ssh -t uno-q '
  cd /home/arduino/lcnc-suite && git pull &&
  cd lcnc-webui && npm run build &&
  cd /home/arduino/canon-grbl-bridge && ./update-webui.sh
'
echo "=== done ==="
EOF
chmod +x /c/Tools/canon-grbl-bridge/deploy-webui.sh