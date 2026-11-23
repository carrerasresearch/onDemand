#!/usr/bin/env bash
# Bioconductor release checker for the OnDemand "Bioconductor" app.
#
# Runs daily from a systemd user timer on a build workstation (see README.md).
# When Bioconductor publishes a release that the app does not offer yet, it:
#   1. builds bioconductor_X.Y.sif from ../rstudio_bioconductor.def with the
#      latest stable RStudio Server,
#   2. uploads it to /ijc/PUBLIC/DATA/images (never overwrites),
#   3. adds the version to form.yml and template/script.sh.erb on a branch,
#   4. runs the app's own job script on a production test2 node and checks
#      rserver, sign-in, the IDE page and the R session,
#   5. opens a pull request and emails the maintainer to review and merge.
# Merging stays manual: master is live for every portal user, and the form
# default is never changed here.
#
# Usage: bioc-release.sh [--dry-run | --test-existing X.Y]
#   --dry-run            only report what would be done
#   --test-existing X.Y  run steps 4-5 (test + email, no PR) for a version the
#                        app already offers, to check the pipeline end to end
#
# Config (not in git; the repo is public): ~/.config/bioc-release/env
#   NOTIFY_EMAIL=...       where to send notifications (required)
#   SSH_HOST=minastirith   cluster login host (submits jobs, sends mail)
#   PARTITION=test2        partition for the smoke test

set -Eeuo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE=run; TEST_VERSION=""
case "${1:-}" in
  --dry-run) MODE=dry ;;
  --test-existing) MODE=test; TEST_VERSION="${2:?version}" ;;
  "") ;;
  *) echo "usage: $0 [--dry-run | --test-existing X.Y]" >&2; exit 2 ;;
esac

# shellcheck disable=SC1090
source "${HOME}/.config/bioc-release/env"
: "${NOTIFY_EMAIL:?set NOTIFY_EMAIL in ~/.config/bioc-release/env}"
SSH_HOST="${SSH_HOST:-minastirith}"
PARTITION="${PARTITION:-test2}"
REPO_SLUG="carrerasresearch/onDemand"
IMAGES_DIR="/ijc/PUBLIC/DATA/images"
STATE="${HOME}/.cache/bioc-release"
REPO="${STATE}/repo"
APP="rstudio_bioconductor_321"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
LOG="${STATE}/logs/${RUN_ID}.log"
mkdir -p "${STATE}/logs" "${STATE}/build"
exec > >(tee -a "${LOG}") 2>&1

log() { echo "[$(date '+%F %T')] $*"; }
rssh() { # ssh to the cluster; drops the login banner, keeps ssh's exit status
  local out rc=0
  out=$(ssh -o BatchMode=yes -o ConnectTimeout=20 "${SSH_HOST}" "$@" 2>&1) || rc=$?
  grep -v '^Identity added\|^Initializing new SSH agent' <<<"${out}" || true
  return "${rc}"
}
notify() { # subject, body
  log "notify: $1"
  printf '%s\n\n-- \nbioc-release.sh on %s, log: %s\n' "$2" "$(hostname)" "${LOG}" \
    | rssh "mail -s '[OOD] $1' '${NOTIFY_EMAIL}'"
}
fail() {
  trap - ERR
  local msg="$1"
  notify "Bioconductor release checker FAILED" \
"${msg}

Manual step: look at the log, fix the cause, then re-run
  systemctl --user start bioc-release.service

Last log lines:
$(tail -n 40 "${LOG}")"
  exit 1
}
trap 'fail "Unexpected error at line ${LINENO}."' ERR

# --- 0. fresh copy of master --------------------------------------------------
if [ ! -d "${REPO}/.git" ]; then
  git clone -q "git@github.com:${REPO_SLUG}.git" "${REPO}"
fi
git -C "${REPO}" fetch -q origin
git -C "${REPO}" checkout -q -B master origin/master
git -C "${REPO}" clean -qfd
cd "${REPO}/${APP}"

configured=$(grep -oE '^ +"[0-9]+\.[0-9]+" =>' template/script.sh.erb | grep -oE '[0-9]+\.[0-9]+' | sort -V)
log "app offers: $(echo ${configured})"

# --- 1. latest Bioconductor release -------------------------------------------
read -r BIOC BIOC_TAG < <(python3 - <<'EOF'
import json, re, urllib.request
url = "https://hub.docker.com/v2/repositories/bioconductor/tidyverse/tags?page_size=100"
best = None
while url:
    d = json.load(urllib.request.urlopen(url, timeout=30))
    for t in d["results"]:
        m = re.fullmatch(r"(\d+)\.(\d+)-R-(\d+)\.(\d+)\.(\d+)", t["name"])
        if m:
            k = tuple(map(int, m.groups()))
            if best is None or k > best[0]:
                best = (k, t["name"])
    url = d.get("next")
print(f"{best[0][0]}.{best[0][1]}", best[1])
EOF
)
log "latest Bioconductor release image: ${BIOC_TAG}"

if [ "${MODE}" = test ]; then
  BIOC="${TEST_VERSION}"
  grep -qx "${BIOC}" <<<"${configured}" || fail "--test-existing ${BIOC}: the app does not offer it."
else
  if grep -qx "${BIOC}" <<<"${configured}"; then
    log "Bioconductor ${BIOC} is already offered; nothing to do."; exit 0
  fi
  BRANCH="bioc-release-${BIOC}"
  if git ls-remote --exit-code --heads origin "${BRANCH}" >/dev/null 2>&1; then
    log "branch ${BRANCH} already exists (PR pending review); nothing to do."; exit 0
  fi
fi

# --- 2. latest stable RStudio Server ------------------------------------------
loc=$(curl -sSI -m 30 "https://rstudio.org/download/latest/stable/server/jammy/rstudio-server-latest-amd64.deb" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}' | tail -1)
RSTUDIO=$(basename "${loc}" | sed -nE 's/^rstudio-server-([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+)-amd64\.deb$/\1+\2/p')
[ -n "${RSTUDIO}" ] || fail "Could not determine the latest RStudio Server version (redirect: ${loc:-none})."
R_VER="${BIOC_TAG##*-R-}"
log "plan: Bioconductor ${BIOC}, R ${R_VER}, RStudio ${RSTUDIO}"

if [ "${MODE}" = dry ]; then
  log "dry run: would build ${IMAGES_DIR}/bioconductor_${BIOC}.sif and open a PR."; exit 0
fi

SIF="bioconductor_${BIOC}.sif"

if [ "${MODE}" = run ]; then
  # --- 3. build ----------------------------------------------------------------
  if rssh "test -e '${IMAGES_DIR}/${SIF}' && echo EXISTS" | grep -q EXISTS; then
    fail "${IMAGES_DIR}/${SIF} already exists but the app does not offer ${BIOC}. Not overwriting it; check who put it there, then delete it or add the version by hand."
  fi
  log "building ${SIF}"
  apptainer build --force --fakeroot \
    --build-arg BIOC_TAG="${BIOC_TAG}" --build-arg RSTUDIO_VERSION="${RSTUDIO}" \
    "${STATE}/build/${SIF}" rstudio_bioconductor.def
  versions=$(apptainer exec "${STATE}/build/${SIF}" cat /etc/ood-image-versions)
  log "image versions: $(echo ${versions})"
  grep -qx "bioconductor=${BIOC}" <<<"${versions}" || fail "Built image does not report Bioconductor ${BIOC}:
${versions}"

  # --- 4. upload (atomic, no overwrite) ---------------------------------------
  sum=$(sha256sum "${STATE}/build/${SIF}" | cut -d' ' -f1)
  scp -q "${STATE}/build/${SIF}" "${SSH_HOST}:${IMAGES_DIR}/.${SIF}.part"
  rsum=$(rssh "sha256sum '${IMAGES_DIR}/.${SIF}.part'" | cut -d' ' -f1)
  [ "${sum}" = "${rsum}" ] || fail "Checksum mismatch after upload (${sum} vs ${rsum})."
  # The login node refuses to exec mv; python's rename works on the CIFS share.
  rssh "python3 -c 'import os; os.rename(\"${IMAGES_DIR}/.${SIF}.part\", \"${IMAGES_DIR}/${SIF}\")'"
  log "uploaded ${IMAGES_DIR}/${SIF} (sha256 ${sum})"
  rm -f "${STATE}/build/${SIF}"

  # --- 5. add the version to the app (newest first; default unchanged) ---------
  git checkout -q -b "${BRANCH}"
  python3 - "${BIOC}" "${R_VER}" "${RSTUDIO}" <<'EOF'
import re, sys
bioc, r, rstudio = sys.argv[1:]
rs_short = ".".join(rstudio.split("+")[0].split(".")[:2])
r_short = ".".join(r.split(".")[:2])
f = "form.yml"; s = open(f).read()
s, n = re.subn(r'(  bioc_version:\n(?:    .*\n)*?    options:\n)',
               lambda m: m.group(1) + f'      - ["{bioc} (R {r_short}, RStudio {rs_short})", "{bioc}"]\n', s, count=1)
assert n == 1, "form.yml: bioc_version options not found"
open(f, "w").write(s)
f = "template/script.sh.erb"; s = open(f).read()
s, n = re.subn(r'(  images = \{\n)',
               lambda m: m.group(1) + f'    "{bioc}" => ["/ijc/PUBLIC/DATA/images/bioconductor_{bioc}.sif",        "rstudio_server_bioconductor_{bioc}"],\n', s, count=1)
assert n == 1, "script.sh.erb: images table not found"
open(f, "w").write(s)
EOF
  git diff --stat
fi

# --- 6. smoke test on a production node ---------------------------------------
log "smoke test of ${BIOC} on ${SSH_HOST}/${PARTITION}"
T="${STATE}/test-${RUN_ID}"; mkdir -p "${T}"
cp -r template/bin "${T}/"
cp "${SELF_DIR}/probe.py" "${T}/"
docker run --rm -v "${PWD}:/app:ro" -w /app ruby:3-slim ruby -rerb -rostruct -e \
  'context=OpenStruct.new(bioc_version: ARGV[0]); print ERB.new(File.read("template/script.sh.erb"), trim_mode: "-").result(binding)' \
  "${BIOC}" > "${T}/script.sh"
grep -q "^  echo \"Bioconductor ${BIOC}:" "${T}/script.sh" || fail "Rendered script.sh is not for Bioconductor ${BIOC}."
WORKDIR_NAME=$(sed -nE 's#^workdir="\$\{HOME\}/([^"]+)"$#\1#p' "${T}/script.sh")
[ -n "${WORKDIR_NAME}" ] || fail "Could not find the workdir in the rendered script.sh."
cat > "${T}/test.sbatch" <<'EOF'
#!/bin/bash
#SBATCH -J ood_bioc_release_test
#SBATCH -c 2
#SBATCH --mem=6G
#SBATCH -t 00:20:00
#SBATCH -o test.out
cd "$SLURM_SUBMIT_DIR"
echo "node=$(hostname)"
export port=$(python3 -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1])')
bash ./script.sh > rserver.log 2>&1 &
pid=$!
python3 ./probe.py "$port" "$USER"
kill $pid; pkill -u "$USER" -f "rserver --www-port $port" 2>/dev/null; wait
echo PROBE_DONE
EOF
remote="ood_bioc_release_test_${RUN_ID}"
# tar is not executable on the login node; copy the files one by one.
rssh "mkdir -p ${remote}/bin"
scp -q "${T}/script.sh" "${T}/test.sbatch" "${T}/probe.py" "${SSH_HOST}:${remote}/"
scp -q "${T}/bin/"* "${SSH_HOST}:${remote}/bin/"
jobid=$(rssh "cd ${remote} && sbatch --parsable -p ${PARTITION} test.sbatch" | tail -1)
log "submitted job ${jobid}"
for _ in $(seq 1 120); do
  st=$(rssh "squeue -h -j ${jobid} -o %T 2>/dev/null" | tail -1)
  [ -z "${st}" ] && break
  sleep 30
done
result=$(rssh "cat ${remote}/test.out 2>/dev/null")
rssh "rm -rf ${remote}" >/dev/null || true
log "test output:"; echo "${result}"
if ! { grep -q '^sign_in: 200' <<<"${result}" && grep -q '^ide: 200 .*title: RStudio' <<<"${result}" \
       && grep -q "rsession --r-libs-user .*/${WORKDIR_NAME}/R/user-libs" <<<"${result}"; }; then
  fail "Smoke test of Bioconductor ${BIOC} failed on ${PARTITION} (job ${jobid}).
${result}"
fi
summary=$(grep -E '^(node=|listening|sign_in|do_sign_in|ide:|rsession:)' <<<"${result}" | cut -c1-160)

if [ "${MODE}" = test ]; then
  notify "Bioconductor ${BIOC}: pipeline test passed" \
"Test run only (--test-existing ${BIOC}); nothing was built, uploaded or changed.

${summary}"
  exit 0
fi

# --- 7. pull request + email --------------------------------------------------
git -c user.name="bioc-release bot" -c user.email="$(git config user.email)" \
  commit -qam "Bioconductor app: add Bioconductor ${BIOC} (R ${R_VER}, RStudio ${RSTUDIO})

Image ${IMAGES_DIR}/${SIF} built from rstudio_bioconductor.def
(bioconductor/tidyverse:${BIOC_TAG}, RStudio Server ${RSTUDIO}) and
smoke-tested on ${PARTITION} by ${APP}/release/bioc-release.sh."
git push -q -u origin "${BRANCH}"
pr=$(gh pr create -R "${REPO_SLUG}" --base master --head "${BRANCH}" \
  --title "Bioconductor app: add Bioconductor ${BIOC}" --body "Automated by \`${APP}/release/bioc-release.sh\`.

- Image: \`${IMAGES_DIR}/${SIF}\` (sha256 \`${sum}\`), from \`bioconductor/tidyverse:${BIOC_TAG}\` with RStudio Server ${RSTUDIO}
- Adds **${BIOC}** to the version dropdown. The default version is unchanged.

Smoke test (job ${jobid}, partition \`${PARTITION}\`):
\`\`\`
${summary}
\`\`\`

Merging deploys to the production portal in place (no restart). After merging, consider announcing the new version and, later, making it the default in \`form.yml\`.")
notify "Bioconductor ${BIOC} is ready: review and merge" \
"Bioconductor ${BIOC} (R ${R_VER}, RStudio ${RSTUDIO}) was built, uploaded and passed the smoke test.

Manual step: review and merge the pull request:
  ${pr}

Merging publishes it on the production portal (in place, no restart). The default version stays as it is.

${summary}"
log "done: ${pr}"
