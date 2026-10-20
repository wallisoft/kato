#!/usr/bin/env bash
#
# Kato installer for Linux.
#   curl -fsSL https://github.com/wallisoft/kato-releases/releases/latest/download/install.sh | bash
# Installs into your home folder; asks for your password only for system libraries
# and firewall ports. Re-run to update.
#
# Works on Debian/Ubuntu/Mint, Fedora, Arch and openSUSE style systems, on X11 and on Wayland.

set -euo pipefail

main() {
    local URL="${KATO_URL:-https://github.com/wallisoft/kato-releases/releases/latest/download/kato-linux-x86_64.tar.gz}"
    local DEST="$HOME/.local/opt/kato"
    local LOG="$HOME/.config/kato/start.log"

    if [ "$(id -u)" -eq 0 ]; then
        echo "Please run this as your normal user, not root. It asks for sudo only when it needs it." >&2
        exit 1
    fi

    echo "==> Installing Kato into $DEST"

    # ---- what Kato needs from the system, by the names each family of distro uses
    #      (the Qt cursor library, desktop notifications, a clipboard tool for X11 and for Wayland, and the two
    #       pieces the Wayland main-computer helper uses: the system Python's GObject support and libei)
    local PM="" PKGS="" INSTALL=""
    if command -v apt-get >/dev/null 2>&1; then
        PM=apt;    PKGS="libxcb-cursor0 libnotify-bin xclip wl-clipboard python3-gi libei1"
        INSTALL="sudo apt-get -o DPkg::Lock::Timeout=180 install -y"      # wait if the system updater is busy
    elif command -v dnf >/dev/null 2>&1; then
        PM=dnf;    PKGS="xcb-util-cursor libnotify xclip wl-clipboard python3-gobject libei";  INSTALL="sudo dnf install -y"
    elif command -v pacman >/dev/null 2>&1; then
        PM=pacman; PKGS="xcb-util-cursor libnotify xclip wl-clipboard python-gobject libei";  INSTALL="sudo pacman -S --needed --noconfirm"
    elif command -v zypper >/dev/null 2>&1; then
        PM=zypper; PKGS="libxcb-cursor0 libnotify-tools xclip wl-clipboard python3-gobject libei1"; INSTALL="sudo zypper --non-interactive install"
    fi

    # (grep reads to the end on purpose: with 'pipefail', a grep that stops early makes the whole test fail at random)
    have_cursor_lib() { ldconfig -p 2>/dev/null | grep 'libxcb-cursor\.so\.0' >/dev/null || ls /usr/lib*/libxcb-cursor.so.0 /usr/lib/*/libxcb-cursor.so.0 >/dev/null 2>&1; }
    have_clip_tool()  { command -v xclip >/dev/null 2>&1 || command -v wl-paste >/dev/null 2>&1; }
    # only matters on Wayland: can the desktop's input capture be used (to be the main computer)?
    have_capture_bits() {
        [ "${XDG_SESSION_TYPE:-}" != "wayland" ] && return 0
        /usr/bin/python3 -c 'import gi' >/dev/null 2>&1 && { ldconfig -p 2>/dev/null | grep 'libei\.so' >/dev/null || ls /usr/lib*/libei.so.* /usr/lib/*/libei.so.* >/dev/null 2>&1; }
    }

    if ! have_cursor_lib || ! command -v xclip >/dev/null 2>&1 || ! command -v wl-paste >/dev/null 2>&1 || ! command -v notify-send >/dev/null 2>&1 || ! have_capture_bits; then
        if [ -n "$PM" ]; then
            echo "==> Need sudo now: installing what Kato needs ($PKGS)"
            $INSTALL $PKGS || echo "    ($PM reported a problem; checking what is actually there)"
        else
            echo "==> This system's package manager isn't one this installer knows."
        fi
    fi

    # ---- never carry on blind: say exactly what is missing and how to get it
    if ! have_cursor_lib; then
        echo >&2
        echo "Kato can't start without the Qt cursor library (libxcb-cursor), and it isn't installed." >&2
        if [ -n "$PM" ]; then echo "Install it with:   $INSTALL $PKGS" >&2; echo "then run this installer again." >&2
        else echo "Install your distro's 'libxcb-cursor' (sometimes 'xcb-util-cursor') package, then run this installer again." >&2; fi
        exit 1
    fi
    local CLIP_WARNING=""
    if ! have_clip_tool; then
        CLIP_WARNING="Clipboard sharing will NOT work until a clipboard tool is installed: ${INSTALL:-install} xclip wl-clipboard"
    fi

    pkill -f "$DEST/kato" 2>/dev/null || true

    # Kato was called AirShare before 0.10. If that is installed here, stop it and take it out: the two would
    # both want the same ports. Its settings are left alone, and Kato picks them up the first time it starts.
    local OLD="$HOME/.local/opt/kato"
    if [ -d "$OLD" ] || [ -e "$HOME/.local/share/applications/kato.desktop" ]; then
        echo "==> Replacing AirShare (Kato's old name); your settings carry over"
        pkill -f "$OLD/kato" 2>/dev/null || true
        sleep 1
        rm -rf "$OLD"
        rm -f "$HOME/.local/bin/kato" "$HOME/.local/share/applications/kato.desktop" "$HOME/.config/autostart/kato.desktop"
    fi

    local tmp
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT
    echo "==> Downloading the latest Kato"
    curl -fsSL "$URL" -o "$tmp/kato.tar.gz"
    rm -rf "$DEST"
    mkdir -p "$DEST"
    tar xzf "$tmp/kato.tar.gz" -C "$DEST" --strip-components=1
    # Older builds name the program kato.bin; make sure "kato" exists either way.
    if [ ! -f "$DEST/kato" ] && [ -f "$DEST/kato.bin" ]; then
        ln -sf "$DEST/kato.bin" "$DEST/kato"
    fi
    if [ ! -x "$DEST/kato" ] && [ ! -L "$DEST/kato" ]; then
        echo "The download doesn't contain the Kato program. Please report this." >&2
        ls "$DEST" >&2
        exit 1
    fi
    chmod +x "$DEST/kato" 2>/dev/null || true

    mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/autostart" "$HOME/.config/kato"
    ln -sf "$DEST/kato" "$HOME/.local/bin/kato"
    cat > "$HOME/.local/share/applications/kato.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=Kato
Comment=One keyboard and mouse across your machines
Exec=$DEST/kato
Icon=$DEST/kato.png
Terminal=false
Categories=Utility;
DESKTOP
    cp "$HOME/.local/share/applications/kato.desktop" "$HOME/.config/autostart/"

    # ---- firewall: Kato's two ports, on whichever firewall this system runs
    if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep "Status: active" >/dev/null; then
        echo "==> Need sudo now: opening Kato's ports in the firewall"
        sudo ufw allow 47810/tcp comment 'Kato' >/dev/null
        sudo ufw allow 47812/udp comment 'Kato discovery' >/dev/null
    elif command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
        echo "==> Need sudo now: opening Kato's ports in the firewall"
        sudo firewall-cmd --quiet --permanent --add-port=47810/tcp --add-port=47812/udp || true
        sudo firewall-cmd --quiet --reload || true
    fi

    # ---- Wayland: let Kato act as a mouse and keyboard, so other machines can drive this one.
    #      One udev rule gives the person logged in at this PC access to /dev/uinput (Steam does the same for
    #      game controllers). It means programs you run can create virtual input devices.
    if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] && [ ! -w /dev/uinput ]; then
        echo "==> Need sudo now: letting Kato act as a keyboard and mouse on Wayland"
        echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
            | sudo tee /etc/udev/rules.d/70-kato-uinput.rules >/dev/null
        echo uinput | sudo tee /etc/modules-load.d/kato-uinput.conf >/dev/null
        sudo modprobe uinput 2>/dev/null || true
        sudo udevadm control --reload-rules 2>/dev/null || true
        sudo udevadm trigger --action=change /dev/uinput 2>/dev/null || sudo udevadm trigger 2>/dev/null || true
        sleep 1
    fi

    # ---- start it, and check that it really is running before saying so
    echo "==> Starting Kato"
    nohup "$DEST/kato" > "$LOG" 2>&1 &
    local i=0 up=""
    while [ $i -lt 10 ]; do
        sleep 1; i=$((i + 1))
        if (exec 3<>/dev/tcp/127.0.0.1/47811) 2>/dev/null; then up=yes; break; fi
        pgrep -f "$DEST/kato" >/dev/null 2>&1 || break
    done
    if [ -z "$up" ] && ! pgrep -f "$DEST/kato" >/dev/null 2>&1; then
        echo >&2
        echo "Kato was installed but did not start. This is what it said:" >&2
        tail -n 15 "$LOG" >&2 || true
        echo >&2
        echo "The full log is at $LOG" >&2
        exit 1
    fi

    echo
    echo "Done. Kato is installed and running, and starts when you log in."

    # ---- say what to expect on THIS desktop, rather than promising a tray icon that may not exist
    local session="${XDG_SESSION_TYPE:-}" desktop="${XDG_CURRENT_DESKTOP:-}"
    case "$desktop" in
        *GNOME*)
            if ! gnome-extensions list --enabled 2>/dev/null | grep -iE 'appindicator|tray' >/dev/null; then
                echo "  - This desktop (GNOME) shows no tray icons, so you won't see Kato's. Open 'Kato' from"
                echo "    your app menu to see its settings. It is running either way."
            fi ;;
        *) echo "  - Look for its dot in your tray, or open 'Kato' from your app menu." ;;
    esac
    if [ "$session" = "wayland" ]; then
        if [ -w /dev/uinput ]; then
            echo "  - This is a Wayland session. Another machine can drive this one. It can also be the main computer"
            echo "    (the one whose keyboard and mouse you share) on GNOME 45 or newer and KDE Plasma 6.1 or newer;"
            echo "    the desktop may ask your permission the first time."
        else
            echo "  - This is a Wayland session, and Kato could not get access to /dev/uinput, so this machine"
            echo "    can't be driven yet. Logging out and in again may be all it needs; otherwise re-run this installer."
        fi
        echo "    Clipboard sharing may be unreliable on Wayland."
    fi
    if [ -n "$CLIP_WARNING" ]; then echo "  - $CLIP_WARNING"; fi
}

main "$@"
