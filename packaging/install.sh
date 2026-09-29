#!/usr/bin/env bash
#
# AirShare installer for Linux (Ubuntu 22.04+ and similar, X11 sessions).
#   curl -fsSL https://github.com/wallisoft/airshare-releases/releases/latest/download/install.sh | bash
# Installs into your home folder; asks for your password only for system libraries
# and firewall ports. Re-run to update.

set -euo pipefail

URL="${AIRSHARE_URL:-https://github.com/wallisoft/airshare-releases/releases/latest/download/airshare-linux-x86_64.tar.gz}"
DEST="$HOME/.local/opt/airshare"

if [ "$(id -u)" -eq 0 ]; then
    echo "Please run this as your normal user, not root. It asks for sudo only when it needs it." >&2
    exit 1
fi

echo "==> Installing AirShare into $DEST"

if command -v apt-get >/dev/null 2>&1; then
    MISSING=()
    for p in libxcb-cursor0 libnotify-bin; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || MISSING+=("$p")
    done
    if [ ${#MISSING[@]} -gt 0 ]; then
        echo "==> Need sudo now: installing ${MISSING[*]}"
        sudo apt-get install -y "${MISSING[@]}" || echo "    (apt reported errors; carrying on)"
    fi
fi

pkill -f "$DEST/airshare" 2>/dev/null || true

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "==> Downloading the latest AirShare"
curl -fsSL "$URL" -o "$tmp/airshare.tar.gz"
rm -rf "$DEST"
mkdir -p "$DEST"
tar xzf "$tmp/airshare.tar.gz" -C "$DEST" --strip-components=1
chmod +x "$DEST/airshare"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/autostart"
ln -sf "$DEST/airshare" "$HOME/.local/bin/airshare"
cat > "$HOME/.local/share/applications/airshare.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=AirShare
Comment=One keyboard and mouse across your machines
Exec=$DEST/airshare
Icon=$DEST/airshare.png
Terminal=false
Categories=Utility;
DESKTOP
cp "$HOME/.local/share/applications/airshare.desktop" "$HOME/.config/autostart/"

if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    echo "==> Need sudo now: opening AirShare's ports in the firewall"
    sudo ufw allow 47810/tcp comment 'AirShare' >/dev/null
    sudo ufw allow 47812/udp comment 'AirShare discovery' >/dev/null
fi

echo "==> Starting AirShare"
nohup "$DEST/airshare" >/dev/null 2>&1 &
echo
echo "Done. AirShare is in your tray and app menu, and starts when you log in."
