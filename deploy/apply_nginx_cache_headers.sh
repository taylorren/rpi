#!/usr/bin/env bash
#
# Apply the cache-header change to the live nginx site config in place, without
# losing the 443 block certbot added.
#
# Run from the workstation:
#
#     ssh -t go4pro 'bash ~/rpi/apply_nginx_cache_headers.sh'
#
# One sudo password, then: backup, edit, `nginx -t`, reload - and if the test
# fails the backup is restored before the script exits, so a bad edit cannot
# take the site down.
#
# Why this script exists rather than a `sudo cp` of the staged file: certbot
# rewrote the live config (listen 443 ssl, certificate paths, the port-80
# redirect server block), and the repo copy has none of that. Copying it over
# would quietly take rpi.go4pro.org off https. This edits exactly the block
# that is changing and leaves every other line alone, so it is also idempotent:
# a second run reports that the block is already present and does nothing.
set -euo pipefail

CONF=/etc/nginx/sites-available/rpi
STAGED="$HOME/rpi/nginx-rpi.go4pro.org.conf"

if [ ! -f "$STAGED" ]; then
    echo "missing $STAGED - scp deploy/nginx-rpi.go4pro.org.conf there first" >&2
    exit 1
fi

if grep -q 'location = /index.html' "$CONF"; then
    echo "already applied: $CONF has the index.html cache block"
    exit 0
fi

MERGED="$(mktemp)"
trap 'rm -f "$MERGED"' EXIT

# Produces $MERGED = the live config with the old comment+location block replaced
# by the new comment + both location blocks from the staged file. Everything
# certbot wrote sits outside that span and is copied through untouched.
python3 - "$CONF" "$STAGED" "$MERGED" <<'PY'
import sys

live_path, staged_path, out_path = sys.argv[1:4]
live = open(live_path, encoding="utf-8").read()
staged = open(staged_path, encoding="utf-8").read()

# The block as it stands on the host today: its comment, then the location,
# ending at the closing brace at indentation 4.
start = live.index("    # The data file is regenerated on every pipeline run")
open_brace = live.index("    location = /data/rpi.json {", start)
end = live.index("\n    }\n", open_brace) + len("\n    }\n")
old = live[start:end]

# The replacement: everything the staged file carries from the new comment to the
# end of its last location block - i.e. minus the server block's own closing brace,
# which is not part of the span being replaced.
new = staged[staged.index("    # Both published files are regenerated"):]
new = new.rstrip()
new = new[: new.rindex("\n}")] + "\n"   # drop the server { } closer
new = new[: new.rindex("    }") + len("    }")] + "\n"

merged = live.replace(old, new, 1)
if merged == live:
    raise SystemExit("could not find the old cache block - inspect the config by hand")

with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(merged)
print(f"block replaced: {len(old)} chars -> {len(new)} chars; {len(merged)} total")
PY

BACKUP="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
sudo cp "$CONF" "$BACKUP"
echo "backup: $BACKUP"

sudo cp "$MERGED" "$CONF"
if ! sudo nginx -t; then
    echo "nginx -t FAILED - restoring $BACKUP" >&2
    sudo cp "$BACKUP" "$CONF"
    exit 1
fi
sudo systemctl reload nginx
echo "reloaded"

# Prove the change landed and nothing else moved: both URLs must answer 200 and
# both must now carry the no-cache header.
for url in / /data/rpi.json; do
    curl -sI "https://rpi.go4pro.org$url" |
        grep -iE '^(HTTP/|cache-control:)' | sed "s|^|${url} |"
done
