#!/usr/bin/env bash
# NanoSnip installer — per-user, into ~/.local. Works from any shell, fish included:
#
#   ./install.sh               install or update
#   ./install.sh --no-deps     skip the system-package step
#   ./install.sh --uninstall   remove everything this script put down
#
# On Arch / CachyOS it installs the system packages with pacman. Elsewhere it
# checks for them and tells you what's missing.
set -euo pipefail

APP_ID="nanosnip"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC_DIR}/nanosnip.py"
ICON_SRC="${SRC_DIR}/assets/nanosnip.svg"

LIB_DIR="${HOME}/.local/share/${APP_ID}"
BIN="${HOME}/.local/bin/${APP_ID}"
DESKTOP="${HOME}/.local/share/applications/${APP_ID}.desktop"
ICON="${HOME}/.local/share/icons/hicolor/scalable/apps/${APP_ID}.svg"

say()  { printf '\033[1;34m::\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m  x\033[0m %s\n' "$1" >&2; exit 1; }

refresh_caches() {
    update-desktop-database "${HOME}/.local/share/applications" &>/dev/null || true
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" &>/dev/null || true
}

# ------------------------------------------------------------- uninstall ----
if [[ "${1:-}" == "--uninstall" ]]; then
    for target in "${LIB_DIR}" "${BIN}" "${DESKTOP}" "${ICON}"; do
        [[ -e "${target}" ]] && rm -rf "${target}"
    done
    refresh_caches
    ok "NanoSnip is gone."
    exit 0
fi

# ------------------------------------------------------------- validate -----
[[ -f "${SRC}" ]] || die "nanosnip.py not found next to this script (${SRC_DIR})."
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "${SRC}" \
    || die "nanosnip.py has a syntax error and would not start."
ok "Source is valid"

# ------------------------------------------------------------- packages -----
if [[ "${1:-}" != "--no-deps" ]] && command -v pacman &>/dev/null; then
    NEEDED=()
    for pkg in pyside6 qt6-multimedia-ffmpeg ffmpeg inter-font desktop-file-utils; do
        pacman -Qq "${pkg}" &>/dev/null || NEEDED+=("${pkg}")
    done
    if ((${#NEEDED[@]})); then
        say "Installing: ${NEEDED[*]}"
        sudo pacman -S --needed "${NEEDED[@]}"
    else
        ok "All packages present"
    fi
fi

python3 -c 'import PySide6.QtWidgets' &>/dev/null \
    || die "PySide6 is missing. Install it from your distro, or: pip install --user PySide6"
command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null \
    || die "ffmpeg is missing. Install it from your distro's packages."
python3 -c 'import PySide6.QtMultimedia' &>/dev/null \
    || warn "Qt Multimedia not found — NanoSnip will preview video as still frames."

# ---------------------------------------------------------------- install ---
install -Dm644 "${SRC}" "${LIB_DIR}/nanosnip.py"
install -Dm644 "${ICON_SRC}" "${ICON}"

mkdir -p "$(dirname "${BIN}")"
cat > "${BIN}" <<EOF
#!/usr/bin/env bash
# Silence the FFmpeg backend's stream dump on startup.
export QT_LOGGING_RULES="qt.multimedia.*=false"
exec python3 "${LIB_DIR}/nanosnip.py" "\$@"
EOF
chmod 755 "${BIN}"

mkdir -p "$(dirname "${DESKTOP}")"
cat > "${DESKTOP}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=NanoSnip
GenericName=Video Editor
Comment=Mirror, trim and crop photos and video
Exec=${BIN} %f
Icon=${APP_ID}
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;
MimeType=video/mp4;video/x-matroska;video/quicktime;video/webm;video/x-msvideo;image/png;image/jpeg;image/webp;
StartupWMClass=nanosnip
Keywords=video;trim;crop;mirror;flip;
EOF

refresh_caches
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${SRC}")
ok "Installed NanoSnip ${VERSION} to ${LIB_DIR}"

if ! command -v "${APP_ID}" &>/dev/null; then
    warn "${HOME}/.local/bin is not on your PATH. Add it once:"
    printf '\n    fish:  fish_add_path ~/.local/bin\n    bash:  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc\n'
fi

cat <<EOF

  Launch:     ${APP_ID}
  With file:  ${APP_ID} ~/Videos/clip.mp4
  Uninstall:  ./install.sh --uninstall

EOF
