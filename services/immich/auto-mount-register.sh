#!/usr/bin/env bash
# Mount a disk by UUID at a given mount point and persist to /etc/fstab.
# Safe to run multiple times (idempotent).
# Usage:
#   sudo ./mount-by-uuid.sh --uuid b8aead62-d7b8-402f-8a5f-d4dba0171617 \
#                           --mountpoint /mnt/external-slim-drive \
#                           [--fstype ext4] [--opts "noatime,nofail,x-systemd.automount"] \
#                           [--owner 1000:1000] [--chmod 755]
#
# Notes:
# - If --fstype is omitted, it is auto-detected via blkid.
# - --owner and --chmod apply to the mount point (not recursively).

set -euo pipefail

UUID=""
MOUNTPOINT=""
FSTYPE=""
FSTAB_OPTS="noatime,nofail,x-systemd.automount"
OWNER=""
CHMOD_MODE=""

err() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "INFO: $*" >&2; }

# --- Parse args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --uuid) UUID="$2"; shift 2;;
    --mountpoint) MOUNTPOINT="$2"; shift 2;;
    --fstype) FSTYPE="$2"; shift 2;;
    --opts) FSTAB_OPTS="$2"; shift 2;;
    --owner) OWNER="$2"; shift 2;;        # e.g. 1000:1000
    --chmod) CHMOD_MODE="$2"; shift 2;;   # e.g. 755
    -h|--help)
      sed -n '1,40p' "$0"; exit 0;;
    *)
      err "Unknown arg: $1 (use --help)";;
  esac
done

[[ $EUID -eq 0 ]] || err "Run as root (use sudo)."
[[ -n "$UUID" ]] || err "--uuid is required."
[[ -n "$MOUNTPOINT" ]] || err "--mountpoint is required."

# --- Detect device & fstype ---
DEV_PATH=$(blkid -o device -t UUID="$UUID" | head -n1 || true)
[[ -n "$DEV_PATH" ]] || err "UUID $UUID not found by blkid."

if [[ -z "$FSTYPE" ]]; then
  FSTYPE=$(blkid -o value -s TYPE -t UUID="$UUID" | head -n1 || true)
  [[ -n "$FSTYPE" ]] || err "Could not detect filesystem type for $UUID. Specify with --fstype."
fi

info "Using device: $DEV_PATH"
info "Filesystem:   $FSTYPE"
info "Mountpoint:   $MOUNTPOINT"
info "fstab opts:   $FSTAB_OPTS"

# --- Create mount point ---
mkdir -p "$MOUNTPOINT"

# --- Backup fstab once per run ---
cp /etc/fstab "/etc/fstab.bak-$(date +%F_%H%M%S)"

# --- Insert or replace fstab entry for this UUID ---
FSTAB_LINE="UUID=${UUID} ${MOUNTPOINT} ${FSTYPE} ${FSTAB_OPTS} 0 2"

# If an exact UUID= line exists, replace it; else append.
if grep -Eq "^UUID=${UUID}[[:space:]]" /etc/fstab; then
  info "Updating existing /etc/fstab entry for UUID=${UUID}..."
  # Use temp file to avoid sed -i portability quirks
  tmpfile=$(mktemp)
  awk -v uuid="$UUID" -v line="$FSTAB_LINE" '
    BEGIN{replaced=0}
    {
      if ($0 ~ "^UUID=" uuid "[[:space:]]") {
        print line; replaced=1
      } else {
        print $0
      }
    }
    END{
      if (replaced==0) {
        # Should not happen because grep found a line, but keep safe:
        print line
      }
    }
  ' /etc/fstab > "$tmpfile"
  cat "$tmpfile" > /etc/fstab
  rm -f "$tmpfile"
else
  info "Appending new entry to /etc/fstab..."
  echo "$FSTAB_LINE" >> /etc/fstab
fi

# --- Reload systemd + mount ---
systemctl daemon-reload
# Create systemd mount unit on first access; ensure mount happens now:
if mountpoint -q "$MOUNTPOINT"; then
  info "Already mounted: $MOUNTPOINT"
else
  # Try direct mount first; if automount is in use, access the path to trigger it
  if ! mount "$MOUNTPOINT" 2>/dev/null; then
    info "Triggering systemd automount by accessing $MOUNTPOINT..."
    ls "$MOUNTPOINT" >/dev/null 2>&1 || true
    # Validate mount succeeded
    if ! mountpoint -q "$MOUNTPOINT"; then
      err "Failed to mount $MOUNTPOINT. Check /etc/fstab and journalctl -xe."
    fi
  fi
fi

# --- Ownership / mode (non-recursive by default) ---
if [[ -n "$OWNER" ]]; then
  chown "$OWNER" "$MOUNTPOINT"
fi
if [[ -n "$CHMOD_MODE" ]]; then
  chmod "$CHMOD_MODE" "$MOUNTPOINT"
fi

info "Mounted OK: $(df -h --output=source,fstype,size,used,avail,target | grep " ${MOUNTPOINT}$" || true)"
info "Done."
