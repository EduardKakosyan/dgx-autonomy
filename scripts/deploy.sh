#!/usr/bin/env bash
# Sync this repository to the DGX and prepare it as jim. Nothing here needs root.
#
#   scripts/deploy.sh                     # rsync, uv sync, write containers/.env
#   scripts/deploy.sh --dry-run           # show what rsync would change, touch nothing
#
# Image builds and starting the controller need Docker access, which jim does not
# have. The script ends by printing those privileged steps for the operator; it
# never runs sudo.
set -euo pipefail

HOST="${DGX_HOST:-hugo-dgx1}"
DEST="${DGX_DEST:-dgx-autonomy}"            # relative to jim's home on the DGX
DATA_DIR="${DGX_AUTONOMY_DATA:-/var/lib/dgx-autonomy}"
MODELS_DIR="${DGX_AUTONOMY_MODELS:-/home/jim/models}"
UV="\$HOME/.local/bin/uv"                   # bare `uv` is not on the non-interactive SSH PATH

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dry_run=""
[[ "${1:-}" == "--dry-run" ]] && dry_run="--dry-run"

echo "==> rsync ${here}/ -> ${HOST}:${DEST}/ ${dry_run}"
rsync -az --delete ${dry_run} --itemize-changes \
  --exclude .venv/ --exclude __pycache__/ --exclude .mypy_cache/ \
  --exclude .pytest_cache/ --exclude .ruff_cache/ --exclude containers/.env \
  "${here}/" "${HOST}:${DEST}/"

if [[ -n "${dry_run}" ]]; then
  exit 0
fi

echo "==> uv sync and containers/.env on ${HOST}"
ssh "${HOST}" bash -s <<EOF
set -euo pipefail
cd "\$HOME/${DEST}"
${UV} sync --frozen
mkdir -p "\$HOME/.local/bin"
ln -sf "\$HOME/${DEST}/.venv/bin/dgx-autonomy" "\$HOME/.local/bin/dgx-autonomy"
cat > containers/.env <<ENV
DGX_AUTONOMY_DATA=${DATA_DIR}
DGX_AUTONOMY_MODELS=${MODELS_DIR}
DGX_AUTONOMY_OPERATOR_UID=\$(id -u)
DGX_AUTONOMY_OPERATOR_GID=\$(id -g)
ENV
echo "wrote \$HOME/${DEST}/containers/.env:"
sed 's/^/  /' containers/.env
EOF

cat <<EOF

==> Privileged steps for the operator (run on ${HOST}; not run by this script):

  sudo install -d -m 0755 ${DATA_DIR}
  cd ~jim/${DEST}/containers
  sudo docker compose --profile images build
  sudo docker compose up -d controller
  sudo docker compose logs --tail 20 controller

Then, as jim:  dgx-autonomy status
EOF
