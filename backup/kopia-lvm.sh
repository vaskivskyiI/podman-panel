#!/usr/bin/env bash
# Kopia snapshot root action: back up a source from an LVM snapshot of the root LV, so live databases
# (PostgreSQL, SQLite) are copied as one crash-consistent point in time instead of file by file.
# Set as both --before-snapshot-root-action and --after-snapshot-root-action (mode essential, the default,
# so a failed snapshot stops the backup). Kopia runs one source at a time; do not run two
# `kopia snapshot create` at once, because "before" removes leftover kopia-* snapshots.
set -euo pipefail

VG=ubuntu-vg
LV=ubuntu-lv
COW_SIZE=20G   # writes to / during the backup that the snapshot can absorb; free space in $VG

id=${KOPIA_SNAPSHOT_ID:?run by kopia}
src=${KOPIA_SOURCE_PATH:?run by kopia}
name=kopia-$id
mnt=/mnt/$name

remove_snapshot() {   # $1: LV name
  local m=/mnt/$1
  if mountpoint -q "$m"; then umount "$m"; fi
  if [[ -d $m ]]; then rmdir "$m"; fi
  if lvs "$VG/$1" >/dev/null 2>&1; then lvremove -q -f "$VG/$1" >&2; fi
}

case ${KOPIA_ACTION:-} in
  before-snapshot-root)
    # A source on another filesystem would show up as an empty folder in the snapshot and be backed
    # up as empty; refuse instead.
    root_dev=$(findmnt -no SOURCE /)
    src_dev=$(findmnt -no SOURCE --target "$src")
    if [[ $src_dev != "$root_dev" ]]; then
      echo "$src is on $src_dev, not on $root_dev; cannot back it up from an LVM snapshot" >&2
      exit 1
    fi
    for old in $(lvs --noheadings -o lv_name "$VG" | awk '$1 ~ /^kopia-/ {print $1}'); do
      echo "removing leftover snapshot $old" >&2
      remove_snapshot "$old"
    done
    lvcreate -q -L "$COW_SIZE" -s -n "$name" "$VG/$LV" >&2
    mkdir -p "$mnt"
    # ro still replays the ext4 journal (into the snapshot), which gives the crash-consistent state.
    mount -o ro "/dev/$VG/$name" "$mnt"
    echo "KOPIA_SNAPSHOT_PATH=$mnt$src"
    ;;
  after-snapshot-root)
    remove_snapshot "$name"
    ;;
  *)
    echo "unexpected KOPIA_ACTION '${KOPIA_ACTION:-}'" >&2
    exit 1
    ;;
esac
