#!/usr/bin/env bash
# Install the Phase 3 host pieces on the DGX. Run by the operator, with sudo, after
# reviewing every file in this directory:
#
#   sudo ~jim/dgx-autonomy/host/install.sh
#
# What it does (idempotent; it neither stops nor starts claude-qwen):
#   /etc/dgx-autonomy/nftables-autonomy.nft          the egress policy
#   /usr/local/sbin/dgx-autonomy-egress              loads/unloads it, writes the marker
#   /etc/systemd/system/dgx-autonomy-egress.service  enabled and started now
#   /usr/local/sbin/dgx-autonomy-reservation         reserve/release/status helper
#   /usr/local/lib/dgx-autonomy/busy_notice.py       the 503 listener
#   /etc/systemd/system/dgx-autonomy-busy-notice.service  enabled; runs only while
#                                                    a reservation is held
#   /etc/sudoers.d/dgx-autonomy                      checked with visudo first
#
# Every installed file is root-owned and not writable by jim: sudo runs the
# reservation helper as root, so jim must not be able to change it.
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg="$here/../src/dgx_autonomy"

echo "==> checking the sudoers entry"
visudo -cf "$here/sudoers-autonomy"

echo "==> checking the egress rules (nft -c)"
nft -c -f "$here/nftables-autonomy.nft"

echo "==> installing files"
install -d -m 0755 -o root -g root /etc/dgx-autonomy /usr/local/lib/dgx-autonomy
install -m 0644 -o root -g root "$here/nftables-autonomy.nft" /etc/dgx-autonomy/nftables-autonomy.nft
install -m 0755 -o root -g root "$here/dgx-autonomy-egress" /usr/local/sbin/dgx-autonomy-egress
install -m 0755 -o root -g root "$pkg/reservation.py" /usr/local/sbin/dgx-autonomy-reservation
install -m 0644 -o root -g root "$here/busy_notice.py" /usr/local/lib/dgx-autonomy/busy_notice.py
install -m 0644 -o root -g root "$here/dgx-autonomy-egress.service" /etc/systemd/system/
install -m 0644 -o root -g root "$here/dgx-autonomy-busy-notice.service" /etc/systemd/system/
install -m 0440 -o root -g root "$here/sudoers-autonomy" /etc/sudoers.d/dgx-autonomy

echo "==> enabling units"
systemctl daemon-reload
systemctl enable dgx-autonomy-egress.service dgx-autonomy-busy-notice.service
# restart, not start: re-applies changed rules and refreshes the marker.
systemctl restart dgx-autonomy-egress.service

echo "==> loaded:"
nft list table inet dgx_autonomy | sed 's/^/    /'
cat /var/lib/dgx-autonomy/policy/egress.json
sudo -u jim sudo -n -l /usr/local/sbin/dgx-autonomy-reservation status > /dev/null \
  && echo "jim may run: dgx-autonomy-reservation reserve|release|status"

cat <<'EOF'

==> The egress rules match the bridges by name (dgx-egress, dgx-internal). If the
    compose networks predate those names, recreate them (README, operator setup step 6):

      cd ~jim/dgx-autonomy/containers
      sudo docker ps -a --filter label=dgx-autonomy.role --format '{{.Names}}'  # review
      sudo docker rm -f $(sudo docker ps -aq --filter label=dgx-autonomy.role)
      sudo docker compose down
      sudo docker compose --profile images build controller
      sudo docker compose up -d controller
EOF
