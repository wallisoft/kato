#!/usr/bin/env bash
# linux-package.sh — turn a Nuitka standalone bundle into a .deb and a no-FUSE AppImage.
# Reusable across Wallisoft apps: Bob runs it as a `command` build; Kato's release.yml runs the same.
#
#   linux-package.sh --bundle DIR --name kato --app "Kato KVM" --version 1.0.1 \
#                    --exe kato --icon kato.png --desc "One keyboard and mouse for all your computers" \
#                    [--maintainer "Wallisoft <hello@tiny-web.uk>"] [--categories "Utility;"] \
#                    [--depends "libc6"] [--out dist] [--deb] [--appimage]
#
# With neither --deb nor --appimage, it builds both. Tools (appimagetool, uruntime) are
# fetched into ~/.cache/wallisoft-pkg the first time, so CI and any build machine just work.
set -euo pipefail

MAINTAINER="Wallisoft <hello@tiny-web.uk>"
CATEGORIES="Utility;"
DEPENDS=""
OUT="dist"
WANT_DEB=0; WANT_APPIMAGE=0
DESC="A Wallisoft app"
EXE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --app) APP="$2"; shift 2;;
    --version) VERSION="$2"; shift 2;;
    --exe) EXE="$2"; shift 2;;
    --icon) ICON="$2"; shift 2;;
    --desc) DESC="$2"; shift 2;;
    --maintainer) MAINTAINER="$2"; shift 2;;
    --categories) CATEGORIES="$2"; shift 2;;
    --depends) DEPENDS="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --deb) WANT_DEB=1; shift;;
    --appimage) WANT_APPIMAGE=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ "$WANT_DEB" = 0 ] && [ "$WANT_APPIMAGE" = 0 ] && { WANT_DEB=1; WANT_APPIMAGE=1; }
: "${BUNDLE:?--bundle required}"; : "${NAME:?--name required}"; : "${VERSION:?--version required}"
APP="${APP:-$NAME}"; EXE="${EXE:-$NAME}"; ICON="${ICON:-}"
[ -d "$BUNDLE" ] || { echo "bundle dir not found: $BUNDLE" >&2; exit 1; }
[ -x "$BUNDLE/$EXE" ] || echo "warning: $BUNDLE/$EXE not found/executable (continuing)" >&2
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"

# --- fetch build tools once ------------------------------------------------
CACHE="${WALLISOFT_PKG_CACHE:-$HOME/.cache/wallisoft-pkg}"; mkdir -p "$CACHE"
AIT="$CACHE/appimagetool"; URT="$CACHE/uruntime"
[ -x "$AIT" ] || { echo "fetching appimagetool…"; wget -q https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage -O "$AIT" && chmod +x "$AIT"; }
[ -x "$URT" ] || { echo "fetching uruntime…";     wget -q https://github.com/VHSgunzo/uruntime/releases/latest/download/uruntime-appimage-squashfs-x86_64 -O "$URT" && chmod +x "$URT"; }

make_desktop() { # $1=exec-value  $2=path
  cat > "$2" <<EOF
[Desktop Entry]
Type=Application
Name=$APP
Comment=$DESC
Exec=$1
Icon=$NAME
Categories=$CATEGORIES
Terminal=false
EOF
}

# --- .deb ------------------------------------------------------------------
if [ "$WANT_DEB" = 1 ]; then
  echo "== building .deb =="
  R="$(mktemp -d)"
  mkdir -p "$R/opt/$NAME" "$R/usr/bin" "$R/usr/share/applications" "$R/usr/share/icons/hicolor/256x256/apps" "$R/DEBIAN"
  cp -a "$BUNDLE/." "$R/opt/$NAME/"
  printf '#!/bin/sh\nexec "/opt/%s/%s" "$@"\n' "$NAME" "$EXE" > "$R/usr/bin/$NAME"
  chmod +x "$R/usr/bin/$NAME"
  [ -n "$ICON" ] && cp "$ICON" "$R/usr/share/icons/hicolor/256x256/apps/$NAME.png"
  make_desktop "$NAME" "$R/usr/share/applications/$NAME.desktop"
  SIZE_KB="$(du -sk "$R" | cut -f1)"
  { echo "Package: $NAME"
    echo "Version: $VERSION"
    echo "Architecture: amd64"
    echo "Maintainer: $MAINTAINER"
    echo "Installed-Size: $SIZE_KB"
    [ -n "$DEPENDS" ] && echo "Depends: $DEPENDS"
    echo "Section: utils"
    echo "Priority: optional"
    echo "Description: $DESC"
  } > "$R/DEBIAN/control"
  DEB="$OUT/${NAME}_${VERSION}_amd64.deb"
  dpkg-deb --build --root-owner-group "$R" "$DEB" >/dev/null
  rm -rf "$R"
  echo "  -> $DEB  ($(du -h "$DEB" | cut -f1))"
fi

# --- AppImage (no-FUSE, uruntime) ------------------------------------------
if [ "$WANT_APPIMAGE" = 1 ]; then
  echo "== building AppImage =="
  A="$(mktemp -d)/AppDir"; mkdir -p "$A"
  cp -a "$BUNDLE/." "$A/"
  [ -n "$ICON" ] && { cp "$ICON" "$A/$NAME.png"; cp "$ICON" "$A/.DirIcon"; }
  make_desktop "$EXE" "$A/$NAME.desktop"
  printf '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/%s" "$@"\n' "$EXE" > "$A/AppRun"
  chmod +x "$A/AppRun"
  OUTAI="$OUT/${APP// /-}-${VERSION}-x86_64.AppImage"
  ARCH=x86_64 "$AIT" --appimage-extract-and-run --runtime-file "$URT" "$A" "$OUTAI" >/dev/null 2>&1
  rm -rf "$(dirname "$A")"
  echo "  -> $OUTAI  ($(du -h "$OUTAI" | cut -f1))"
fi
echo "done."
