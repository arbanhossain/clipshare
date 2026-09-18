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
# Install the package itself, not just its deps: this is what creates the
# `clipshare` console script declared in pyproject.toml.
"${VENV}/bin/pip" install --quiet "${SRC}"

# Earlier versions copied the package into DEST, which is the service's
# WorkingDirectory — that copy shadowed the installed one and went stale.
rm -rf "${DEST:?}/clipshare"

mkdir -p "${HOME}/.local/bin"
ln -sf "${VENV}/bin/${APP}" "${HOME}/.local/bin/${APP}"

SERVICE_DIR="${HOME}/.config/systemd/user"
mkdir -p "${SERVICE_DIR}"
sed -e "s|@PYTHON@|${VENV}/bin/python|" \
    -e "s|@DIR@|${DEST}|" \
    "${SRC}/scripts/clipshare.service" > "${SERVICE_DIR}/clipshare.service"

systemctl --user daemon-reload
systemctl --user enable clipshare.service
# restart, not --now: an already-running daemon keeps serving the old code
systemctl --user restart clipshare.service

echo
echo "Installed. Check status with:  systemctl --user status clipshare"
echo "Then:                          clipshare status"
echo "Open the history window with:  clipshare app"
if ! command -v "${APP}" >/dev/null; then
    echo
    echo "Note: ${HOME}/.local/bin is not on your PATH — add it, or run"
    echo "      ${VENV}/bin/${APP} instead."
fi
