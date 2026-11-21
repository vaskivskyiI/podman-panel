# Encrypted backups with Kopia

Backs up the stack folders (see "Folders" in the main README) of every user, plus `/etc/podman-panel`,
to a remote SFTP server. Kopia encrypts on this host; the backup server only stores encrypted blobs.

Live databases (PostgreSQL, SQLite) sit in `~/podman_data`, so copying files while they run is not
safe. Instead, for each source, `kopia-lvm.sh` takes an LVM snapshot of the root LV, mounts it
read-only, and Kopia reads the source from there. Every database is then backed up as it was at one
instant, like after a power cut, which every database recovers from. Kopia still records the snapshot
under the original path (`/home/alice/podman_data`), so restores and history look normal.

Assumptions: root is ext4 on LV `ubuntu-vg/ubuntu-lv` with at least 20 GB free in the VG (`sudo vgs`),
and the panel checkout is in `/opt/podman-panel` (otherwise copy `kopia-lvm.sh` to a root-owned path
and use that). Kopia runs as root: it needs LVM, and files in `podman_data` belong to the containers'
mapped uids.

## 1. Backup server: SFTP-only account (once)

On the backup server, a dedicated account that can only use SFTP inside its own folder:

```bash
sudo useradd --create-home --shell /usr/sbin/nologin kopia-podman
sudo mkdir -p /srv/kopia/kopia-podman/repo
sudo chown root:root /srv/kopia /srv/kopia/kopia-podman            # chroot must be root-owned
sudo chmod 755 /srv/kopia /srv/kopia/kopia-podman
sudo chown kopia-podman: /srv/kopia/kopia-podman/repo
sudo tee /etc/ssh/sshd_config.d/kopia-podman.conf <<'EOF'
Match User kopia-podman
    ChrootDirectory /srv/kopia/%u
    ForceCommand internal-sftp
    PasswordAuthentication no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
EOF
sudo sshd -t && sudo systemctl reload ssh
```

Put the public key from step 2 in `/home/kopia-podman/.ssh/authorized_keys` (owned by kopia-podman,
mode 600).

## 2. Podman server: install Kopia and connect (once)

```bash
# Kopia from its apt repository
sudo install -d -m 755 /etc/apt/keyrings
curl -s https://kopia.io/signing-key | sudo gpg --dearmor -o /etc/apt/keyrings/kopia-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kopia-keyring.gpg] http://packages.kopia.io/apt/ stable main" \
  | sudo tee /etc/apt/sources.list.d/kopia.list
sudo apt update && sudo apt install kopia

# SSH key for the SFTP account (no passphrase: the timer runs unattended)
sudo ssh-keygen -t ed25519 -N '' -C kopia@$(hostname) -f /root/.ssh/kopia_ed25519
sudo cat /root/.ssh/kopia_ed25519.pub          # -> backup server, step 1
# Pin the backup server's host key; compare the fingerprint with `ssh-keygen -lf` on the backup server
sudo sh -c 'ssh-keyscan -t ed25519 BACKUP_HOST >> /root/.ssh/known_hosts'
sudo ssh-keygen -lf /root/.ssh/known_hosts

# Create the repository (asks for the repository password twice)
sudo kopia repository create sftp --host=BACKUP_HOST --username=kopia-podman --path=/repo \
  --keyfile=/root/.ssh/kopia_ed25519 --known-hosts=/root/.ssh/known_hosts
# Reconnect with actions enabled: Kopia runs no policy actions unless the connection allows them
sudo kopia repository disconnect
sudo kopia repository connect sftp --host=BACKUP_HOST --username=kopia-podman --path=/repo \
  --keyfile=/root/.ssh/kopia_ed25519 --known-hosts=/root/.ssh/known_hosts --enable-actions
```

**Repository password:** Kopia keeps it in `/root/.config/kopia/` so the timer can run. Store a copy in
Bitwarden as well. Without it the backups cannot be decrypted, and the copy on this server is lost
together with the server: exactly when you need the backups.

## 3. Policies (once, and again for each new user)

Retention, compression, and the LVM actions on every source:

```bash
sudo kopia policy set --global --compression=zstd \
  --keep-latest 14 --keep-hourly 0 --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --keep-annual 3

LVM=/opt/podman-panel/backup/kopia-lvm.sh
for src in /home/*/podman /home/*/podman_data /home/*/podman_logs /root/podman /root/podman_data \
           /root/podman_logs /etc/podman-panel; do
  [ -d "$src" ] || continue
  sudo kopia policy set "$src" --before-snapshot-root-action "$LVM" --after-snapshot-root-action "$LVM"
done
# Logs: one week is enough (remove this loop to keep them like everything else)
for src in /home/*/podman_logs /root/podman_logs; do
  [ -d "$src" ] || continue
  sudo kopia policy set "$src" --keep-latest 7 --keep-daily 7 --keep-weekly 0 --keep-monthly 0 --keep-annual 0
done
```

(`/home/*` is expanded by your shell, so folders other users keep private are skipped. Run the loops in
`sudo -i` if `ls /home/*/` shows permission errors.)

## 4. First backup

Back up each source once by name. After that the timer's `--all` includes it: `--all` means every
source this host has backed up before, so **a new user's folders must also be backed up once by hand.**

```bash
sudo -i     # the loop below needs to see every user's folders
for src in /home/*/podman /home/*/podman_data /home/*/podman_logs /root/podman /root/podman_data \
           /root/podman_logs /etc/podman-panel; do
  [ -d "$src" ] && kopia snapshot create "$src"
done
lvs ubuntu-vg     # no kopia-* LV may remain
```

## 5. Daily timer

```bash
sudo tee /etc/systemd/system/kopia-backup.service <<'EOF'
[Unit]
Description=Kopia backup of podman stacks
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=HOME=/root
ExecStart=/usr/bin/kopia snapshot create --all --no-progress
Nice=10
IOSchedulingClass=idle
EOF
sudo tee /etc/systemd/system/kopia-backup.timer <<'EOF'
[Unit]
Description=Daily Kopia backup

[Timer]
OnCalendar=*-*-* 03:30
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now kopia-backup.timer
```

`snapshot create` also runs Kopia's maintenance (which deletes expired data) when it is due.

## Checking

```bash
systemctl list-timers kopia-backup.timer        # last and next run
systemctl status kopia-backup.service           # result of the last run
journalctl -u kopia-backup.service -n 100       # its log
sudo kopia snapshot list --all                  # every snapshot, per source
sudo kopia maintenance info                     # when maintenance last ran
sudo lvs ubuntu-vg                              # no kopia-* LV outside a run
```

The panel's **Backups** tab shows the same (it appears once Kopia is installed): connection, timer,
last snapshot per source, and stack folders that no source covers yet. Problems also show under
Insights. The panel only reads; it runs `kopia repository status` and `kopia snapshot list` as root.

## Restoring

```bash
sudo kopia snapshot list /home/alice/podman_data
# One file or folder
sudo kopia restore <snapshot-id>/myapp/postgres/pgdata/PG_VERSION /tmp/restore/PG_VERSION
# A whole stack's data, next to the live one; stop the stack before swapping folders
sudo kopia restore <snapshot-id>/myapp /tmp/restore/myapp
```

Kopia restores owners and permissions (as root), so the container's mapped uids come back as they were.
To check a PostgreSQL copy without touching the live stack, start a throwaway container on it with the
same image and user mapping as the stack, and look for `database system is ready to accept connections`
after `database system was not properly shut down; automatic recovery in progress`.

## Other Kopia repositories

This setup is the host's own: it runs as root because it needs LVM and every user's files, and root's
Kopia config (`/root/.config/kopia`) is connected to this one repository. Keep it that way.

For anything else (laptops, other folders, other destinations), run Kopia as an ordinary rootless
stack, e.g. `docker.io/kopia/kopia` with `server start --address=0.0.0.0:51515` behind your reverse
proxy, its config and cache in `~/podman_data/kopia/`, and only the folders it should back up mounted
read-only. It has its own repositories and web UI, sees nothing else on the host, and its config is
itself backed up by the host backup above. For a one-off CLI repository as root, give it a separate
config so the host's connection stays untouched: `sudo kopia --config-file=/root/.config/kopia/other.config ...`.

## Ransomware

Whoever controls this server (root) also holds the SSH key and repository password, so they can delete
or overwrite the backups on the backup server. Kopia's encryption protects confidentiality, not that.
Protect the stored data on the backup server's side, out of this server's reach:

- **Filesystem snapshots** of `/srv/kopia` on the backup server (ZFS or btrfs, e.g. daily, kept 30 days,
  taken by a backup-server cron job). Simplest if the backup server already runs ZFS or btrfs.
- **S3 with object lock** (MinIO or Garage on the backup server) instead of SFTP: Kopia supports
  `--retention-mode COMPLIANCE --retention-period 30d` on `repository create s3`, so even a valid key
  cannot delete blobs for that period.
- **Kopia repository server** (`kopia server` on the backup server, holding the storage itself) with ACLs
  that allow this host to add snapshots but not delete them. Verify the ACL behaviour (and that
  maintenance still runs) before relying on it.

Also test a restore now and then: a backup is only proven by restoring it.
