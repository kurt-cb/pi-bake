# nginx setup — pi-bake lab HTTP serving

Purpose: stand up an HTTP server on the lab host that serves
`/var/lib/tftpboot/` so PXE clients (Alpine init via `alpine_repo=`
+ `apkovl=`, plus any other lab tooling) can pull artifacts that
dnsmasq's TFTP can't reasonably stream.

**Target host:** the Fedora 44 laptop running dnsmasq for the
192.168.4.0/24 lab subnet (interface `enp0s3`, IP `192.168.4.2`).

**Why nginx and not python http.server / busybox httpd:** persistent,
SELinux-friendly, runs as a non-root user, restarts cleanly with the
host. Right tool once you commit to "this laptop is the lab PC."

**Operator access principle:** nginx serves whatever exists under
`/var/lib/tftpboot/`. That directory is already `kurt-cb`-writable
(see existing ownership). Edits you make as `kurt-cb` appear in
HTTP immediately — no `sudo`-shuffle to update PXE artifacts.

---

## 1. Install + start

```bash
sudo dnf install -y nginx
sudo systemctl enable --now nginx
systemctl status nginx --no-pager | head -10
```

Expected: `Active: active (running)`. If it fails to bind port 80,
something else is on 80 already (`ss -tlnp | grep :80`). Either stop
that or pick a different port (see §2 — change `listen` directive).

## 2. Drop a pi-bake-lab server block

Create `/etc/nginx/conf.d/pibake-lab.conf`:

```nginx
# pi-bake lab HTTP — serves /var/lib/tftpboot/ over HTTP for
# PXE-boot clients that need apkovl + alpine_repo via URL,
# plus any other lab tooling. Bound to the lab subnet only.

server {
    listen 192.168.4.2:80 default_server;
    server_name _;

    root /var/lib/tftpboot;

    # Lab is volatile — no client-side caching, ever.
    expires -1;
    add_header Cache-Control "no-store, no-cache, must-revalidate" always;

    # Directory listings on. Operator + PXE clients both benefit
    # from being able to GET / and see what's there.
    autoindex on;
    autoindex_exact_size off;
    autoindex_localtime on;

    # Reasonable default mime types are loaded from
    # /etc/nginx/mime.types; .apkovl.tar.gz + .img.xz fall through
    # to application/octet-stream which is correct.

    location / {
        try_files $uri $uri/ =404;
    }

    # Bigger send buffers for the multi-MB modloop/initramfs
    # fetches a PXE client typically makes.
    sendfile on;
    tcp_nopush on;
    sendfile_max_chunk 1m;
}
```

Validate + reload:

```bash
sudo nginx -t                # config syntax check
sudo systemctl reload nginx
```

Bound to `192.168.4.2:80` rather than `0.0.0.0:80` — this keeps it
off your other interfaces. If your lab IP ever changes, update the
`listen` directive.

## 3. SELinux — relabel for shared content

Fedora 44 ships SELinux enforcing. The directory's current type is
`public_content_t` or `tftpdir_t` (dnsmasq-tftp readable). nginx
runs as `httpd_t` which by default cannot read those types.

The cleanest fix: relabel to **`public_content_t`** — that's
SELinux's "intended for both httpd and tftpd to read" type. dnsmasq
keeps working, nginx gets access.

```bash
# Verify current label first (so you know what to restore if needed):
ls -dZ /var/lib/tftpboot/
system_u:object_r:tftpdir_rw_t:s0 /var/lib/tftpboot/

# Add the new fcontext rule + apply:
sudo semanage fcontext -a -t public_content_t "/var/lib/tftpboot(/.*)?"
sudo restorecon -Rv /var/lib/tftpboot

# Verify:
ls -dZ /var/lib/tftpboot/
# Should show: ...:public_content_t:...
```

If you'd rather not relabel (e.g. another tool depends on the
current type), the **fallback** is to flip the `httpd_read_user_content`
boolean and use a symlink from nginx's default webroot — but
relabel is cleaner.

```bash
# Fallback only — skip if relabel above worked.
sudo setsebool -P httpd_read_user_content on
sudo ln -s /var/lib/tftpboot /usr/share/nginx/html/pibake
```

## 4. firewalld — open port 80 on the lab interface only

Fedora's firewalld is on by default. Open HTTP on the zone that
covers the lab interface (`enp0s3`). DON'T open it on the
`public` zone unless you want this exposed on the WAN side.

```bash
# Which zone is enp0s3 in?
sudo firewall-cmd --get-zone-of-interface=enp0s3

# Typically it's `public` — but the lab interface usually wants a
# more permissive `internal` or `trusted` zone. If your lab
# subnet is already segregated (e.g. enp0s3 only sees the lab),
# `public` is fine. Otherwise move the interface to `internal`:
#   sudo nmcli connection modify <conn-name> connection.zone internal
#   sudo nmcli connection up <conn-name>

# Open HTTP in that zone:
sudo firewall-cmd --zone=public --add-service=http --permanent
sudo firewall-cmd --reload

# Verify:
sudo firewall-cmd --zone=public --list-services
```

## 5. Smoke-test

```bash
# Should return 200 + a small file:
curl -sS http://192.168.4.2/test.txt

# Should return 200 + directory listing HTML:
curl -sS http://192.168.4.2/88-a2-9e-44-31-f3/ | head -20

# Should return 200 + a couple MB of the modloop:
curl -sSo /tmp/modloop-smoke.bin \
  http://192.168.4.2/88-a2-9e-44-31-f3/boot/modloop-rpi && \
ls -lh /tmp/modloop-smoke.bin && \
file /tmp/modloop-smoke.bin
# expected: ~33 MB, "Squashfs filesystem, little endian"
```

From a second machine on the lab subnet (e.g. the CM4 once booted):

```bash
curl -sS http://192.168.4.2/test.txt
```

If both of those return content cleanly, nginx is good.

## 6. What pi-bake does with it

Once nginx is live, pi-bake's `os_mode: pxe` work (see
[feature_request.md](feature_request.md)) writes a `cmdline.txt`
of the shape:

```
ip=dhcp alpine_repo=http://192.168.4.2/<cm4-mac>/apks/aarch64 \
  apkovl=http://192.168.4.2/<cm4-mac>/<host>.apkovl.tar.gz \
  modules=loop,squashfs,sd-mod,usb-storage,genet console=tty1
```

— and bakes a network-feature-enabled `initramfs-rpi` so the
initramfs can actually bring up the NIC and fetch those URLs.
That work is captured under
`os_mode-pxe` and `raspbian-pxe-recovery` in
[feature_request.md](feature_request.md); the nginx server is the
prerequisite for both.

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `curl: (7) Failed to connect ... port 80: Connection refused` | nginx not running (`systemctl status nginx`) OR not bound to that IP (`ss -tlnp \| grep :80`). |
| `403 Forbidden` | SELinux denial (`sudo ausearch -m AVC -ts recent \| tail`) — check §3 relabel worked: `ls -dZ /var/lib/tftpboot/` |
| `404 Not Found` for a file that exists | Wrong URL — remember `/var/lib/tftpboot/` is the root, so `http://192.168.4.2/X` maps to `/var/lib/tftpboot/X`. Check `ls /var/lib/tftpboot/X` exists. |
| nginx fails to start with "bind() ... permission denied" | SELinux blocking port 80 bind — `sudo setsebool -P httpd_can_network_connect on`. Or another tool already on 80 (`ss -tlnp \| grep :80`). |
| dnsmasq-tftp stops working after relabel | Restore: `sudo semanage fcontext -d "/var/lib/tftpboot(/.*)?" && sudo restorecon -Rv /var/lib/tftpboot`. Then use the fallback symlink approach in §3. |
| Want to log requests | Already on: `/var/log/nginx/access.log`. `tail -f` it during PXE-boot tests. |

## Removing the setup

If you ever need to roll back cleanly:

```bash
sudo systemctl stop nginx
sudo systemctl disable nginx
sudo rm /etc/nginx/conf.d/pibake-lab.conf
sudo semanage fcontext -d "/var/lib/tftpboot(/.*)?"
sudo restorecon -Rv /var/lib/tftpboot
sudo firewall-cmd --zone=public --remove-service=http --permanent
sudo firewall-cmd --reload
sudo dnf remove -y nginx       # optional
```
