#!/usr/bin/env bash
# cache-images.sh — Pre-cache all netris-lab container images on the jump server.
#
# Called automatically by deploy-jump.sh before the deploy starts.
# Resolves Jinja2 image tags from netris-lab/group_vars/all.yml, then
# pulls each image via skopeo with exponential-backoff retries.
#
# The cache uses the same skopeo dir: format + image-ref marker file as
# netris-lab/roles/cache, so the submodule's cache role skips every
# image it finds pre-cached on the server.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE_DIR="${CACHE_DIR:-${HOME}/.cache/netris-lab/k3s-images}"
MAX_RETRIES="${MAX_RETRIES:-10}"
BASE_DELAY="${BASE_DELAY:-30}"
MAX_DELAY="${MAX_DELAY:-300}"

mkdir -p "$CACHE_DIR"

IMAGES=$(python3 -c "
import yaml, re, sys
with open('${REPO_ROOT}/netris-lab/group_vars/all.yml') as f:
    data = yaml.safe_load(f)
ctrl = data.get('netris_controller_images', {})
imgs = data.get('container_images', [])
if not imgs:
    print('ERROR: no container_images found', file=sys.stderr)
    sys.exit(1)
for img in imgs:
    resolved = re.sub(
        r'\{\{\s*netris_controller_images\.(\w+)\s*\}\}',
        lambda m: str(ctrl.get(m.group(1), m.group(0))),
        img)
    print(resolved)
")

total=0; cached=0; failed_images=""

while IFS= read -r img; do
    total=$((total + 1))
    fname=$(echo "$img" | sed 's|[/:]|_|g')
    dest="${CACHE_DIR}/${fname}"

    if [ -d "$dest" ] && [ -f "${dest}/image-ref" ]; then
        echo "CACHED  $img"
        cached=$((cached + 1))
        continue
    fi

    rm -rf "$dest"
    pulled=false
    delay=$BASE_DELAY
    for attempt in $(seq 1 "$MAX_RETRIES"); do
        if skopeo copy "docker://${img}" "dir:${dest}" 2>&1; then
            echo "$img" > "${dest}/image-ref"
            echo "SAVED   $img (attempt ${attempt})"
            cached=$((cached + 1))
            pulled=true
            break
        fi
        echo "RETRY   $img (attempt ${attempt}/${MAX_RETRIES}, wait ${delay}s)"
        sleep "$delay"
        delay=$((delay * 2))
        [ "$delay" -gt "$MAX_DELAY" ] && delay=$MAX_DELAY
    done
    if [ "$pulled" = "false" ]; then
        rm -rf "$dest"
        echo "FAILED  $img (after ${MAX_RETRIES} attempts)"
        failed_images="${failed_images} ${img}"
    fi
done <<< "$IMAGES"

echo ""
echo "Image cache: ${cached}/${total} ready ($(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1))"
if [ -n "$failed_images" ]; then
    echo "ERROR: Failed to cache:${failed_images}"
    exit 1
fi
