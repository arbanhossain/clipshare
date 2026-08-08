#!/usr/bin/env bash
# Install clipshare for the current user: venv + systemd user service.
set -euo pipefail

APP=clipshare
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${HOME}/.local/share/${APP}"
VENV="${DEST}/venv"

mkdir -p "${DEST}"
if ! command -v python3 >/dev/null; then
    echo "python3 is required" >&2
    exit 1
fi

python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${SRC}/requirements.txt"

cp -r "${SRC}/clipshare" "${DEST}/"

SERVICE_DIR="${HOME}/.config/systemd/user"
mkdir -p "${SERVICE_DIR}"
sed -e "s|@PYTHON@|${VENV}/bin/python|" \
    -e "s|@DIR@|${DEST}|" \
    "${SRC}/scripts/clipshare.service" > "${SERVICE_DIR}/clipshare.service"

systemctl --user daemon-reload
systemctl --user enable --now clipshare.service

echo
echo "Installed. Check status with:  systemctl --user status clipshare"
echo "Open the history window with:  ${VENV}/bin/python -m clipshare app"
