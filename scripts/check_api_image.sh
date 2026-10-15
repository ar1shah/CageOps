#!/usr/bin/env bash
# Check the BUILT API image, not the dev venv (where every package is installed).
#
#   docker build -f infra/docker/api.Dockerfile -t cageops-api:local .
#   scripts/check_api_image.sh cageops-api:local
#
# The pytest guard (services/api/tests/test_no_trainer_import.py) can only prove the API code does
# not import the trainer. This proves the SHIPPED IMAGE does not contain it, and is what CI runs.
# MAX_IMAGE_MB is a budget so growth is noticed (default 800; the image was 612 MiB when set).
set -euo pipefail

image="${1:?usage: check_api_image.sh <image>}"
max_mb="${MAX_IMAGE_MB:-800}"
fail=0
say() { printf '%-58s %s\n' "$1" "$2"; }
bad() { say "$1" "FAIL: $2"; fail=1; }

run_py() { docker run --rm --entrypoint python "$image" -c "$1"; }

# 1. non-root, with a numeric id orchestrators can verify
uid="$(docker run --rm --entrypoint id "$image" -u)"
if [ "$uid" = "10001" ]; then say "runs as a non-root numeric user" "ok (uid $uid)"; else bad "runs as a non-root numeric user" "uid is $uid"; fi

# 2. it can be what it is for
if out="$(run_py 'import cageops_api.main, lightgbm, numpy; print(lightgbm.__version__)' 2>&1)"; then
  say "imports the API, LightGBM and numpy" "ok (lightgbm $out)"
else
  bad "imports the API, LightGBM and numpy" "$out"
fi

# 3. it must NOT contain the training or ingestion code: the import has to fail, and fail for
#    that reason (a broken image must not pass by failing everything)
for module in cageops_trainer cageops_worker cageops_scraper; do
  if out="$(run_py "import $module" 2>&1)"; then
    bad "does not contain $module" "it imported"
  elif printf '%s' "$out" | grep -q "ModuleNotFoundError: No module named '$module'"; then
    say "does not contain $module" "ok (ModuleNotFoundError)"
  else
    bad "does not contain $module" "failed for another reason: $out"
  fi
done

# 4. nothing that should not ship
leftovers="$(docker run --rm --entrypoint sh "$image" -c \
  'find / -xdev \( -name ".env" -o -name ".env.*" -o -name "uv" -o -name "uvx" -o -name "*.kaggle*" \) \
   -not -path "/proc/*" 2>/dev/null; ls /app/*.py /src 2>/dev/null' || true)"
if [ -z "$leftovers" ]; then say "no .env, uv, credentials or source tree" "ok"; else bad "no .env, uv, credentials or source tree" "$leftovers"; fi

# 5. it declares a health check
if [ "$(docker image inspect "$image" --format '{{ if .Config.Healthcheck }}yes{{ end }}')" = "yes" ]; then
  say "declares a HEALTHCHECK" "ok"
else
  bad "declares a HEALTHCHECK" "none"
fi

# 6. size budget
bytes="$(docker image inspect "$image" --format '{{.Size}}')"
mb=$(( bytes / 1048576 ))
if [ "$mb" -le "$max_mb" ]; then say "size within budget (${max_mb} MiB)" "ok (${mb} MiB, ${bytes} bytes)"; else bad "size within budget (${max_mb} MiB)" "${mb} MiB"; fi

exit "$fail"
