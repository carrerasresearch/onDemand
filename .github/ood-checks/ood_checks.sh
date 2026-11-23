# ood_checks.sh - start-up checks and user-facing error reports for the
# OnDemand interactive apps.
#
# Master copy: .github/ood-checks/ood_checks.sh. Each app's template/ has an
# identical copy; run .github/ood-checks/sync.sh after editing this file.
#
# A failed check writes ood_error.md to the session directory, which is on
# /ijc/SERVICES and so still writable when the user's home is full. The app's
# completed.md.erb shows that file on the session card, so users see what
# went wrong and what to do about it without opening output.log.
#
#   source ./ood_checks.sh   # in before.sh (cwd = session directory)
#   ood_check_home           # stop now if $HOME is full or not writable
#   ood_check_image IMAGE    # stop now if the app's image is missing
#   ood_wait_for_port NAME HOST PORT [SECONDS]   # in after.sh, instead of
#                            # wait_until_port_used; reports why it failed
#
# Do not use `set -e` here: before.sh/after.sh are sourced into OOD's job
# script.

export OOD_SESSION_DIR="${OOD_SESSION_DIR:-$(pwd -P)}"
export OOD_ERROR_FILE="${OOD_SESSION_DIR}/ood_error.md"
export OOD_CHECKS="${OOD_SESSION_DIR}/ood_checks.sh"
OOD_MIN_FREE_MB="${OOD_MIN_FREE_MB:-1024}"

_ood_human() { # KiB -> "1.2 TB"
  awk -v k="$1" 'BEGIN { split("KB MB GB TB PB", u); i = 1; while (k >= 1024 && i < 5) { k /= 1024; i++ }
                         printf (k < 10 ? "%.1f %s" : "%.0f %s"), k, u[i] }'
}

# ood_report TITLE BODY - write the session card message (markdown) and log it.
ood_report() {
  {
    printf '#### %s\n\n%s\n\n' "$1" "$2"
    printf '<small>Session %s on %s, %s. If this does not help, send the session ID to the HPC team.</small>\n' \
      "$(basename "$OOD_SESSION_DIR")" "$(hostname)" "$(date '+%F %T')"
  } > "$OOD_ERROR_FILE"
  { echo "ERROR: $1"; echo "$2"; } >&2
}

# ood_fail TITLE BODY - report and end the job.
ood_fail() {
  ood_report "$1" "$2"
  exit 1
}

_ood_home_usage() { # prints "size used avail" (KiB) of the filesystem holding $HOME
  df -Pk "$(readlink -f "$HOME")" 2>/dev/null | awk 'NR == 2 { print $2, $3, $4 }'
}

ood_check_home() {
  local real size used avail probe
  real="$(readlink -f "$HOME")"
  read -r size used avail <<< "$(_ood_home_usage)"
  if [[ -n "$avail" ]] && (( avail < OOD_MIN_FREE_MB * 1024 )); then
    ood_fail "Your home folder is full" \
"The session could not start because your home folder \`$HOME\`$([[ "$real" != "$HOME" ]] && echo " (stored on \`$real\`)") has only **$(_ood_human "$avail") free** of $(_ood_human "$size"). The app needs to write a few small files there to start.

**What to do:** free some space, for example by deleting files you no longer need or moving large data to your group's project folder. To see what takes the space, open *Files* in the portal and sort by size, or run \`du -sh ~/* ~/.[!.]* | sort -h\` in *Clusters → Shell*. Then launch the session again. If you need a bigger quota, ask the HPC team."
  fi
  probe="$HOME/.ood_write_test.$$"
  if ! ( : > "$probe" ) 2>/dev/null; then
    ood_fail "Your home folder is not writable" \
"The session could not start because it cannot create files in your home folder \`$HOME\`$([[ "$real" != "$HOME" ]] && echo " (\`$real\`)").

**What to do:** this is usually a permissions problem on the storage side. Send the session ID below to the HPC team."
  fi
  rm -f "$probe"
}

ood_check_image() {
  if [[ ! -r "$1" ]]; then
    ood_fail "The app's software image is not available" \
"The image \`$1\` is missing or cannot be read from $(hostname), so the app cannot start. This is not caused by anything in your account.

**What to do:** try again in a few minutes, or pick another version in the form if the app offers one. If it keeps failing, send the session ID below to the HPC team."
  fi
}

# Explain a server that did not come up: known causes first, then the log.
_ood_startup_report() { # NAME SECONDS EXITED
  local name="$1" secs="$2" exited="$3" log="${OOD_SESSION_DIR}/output.log" avail hints="" lines
  read -r _ _ avail <<< "$(_ood_home_usage)"
  if [[ -n "$avail" ]] && (( avail < OOD_MIN_FREE_MB * 1024 )); then
    hints+="- **Your home folder is full** (\`$HOME\`, $(_ood_human "$avail") free). Free some space and try again: see *Files* in the portal, or run \`du -sh ~/* | sort -h\` in a shell."$'\n'
  elif grep -qiE 'no space left on device|disk quota exceeded' "$log" 2>/dev/null; then
    hints+="- **A disk or quota was full** (\"No space left on device\"). Your home has $(_ood_human "${avail:-0}") free now; check the paths in the log below (for example a project folder) and free space there."$'\n'
  fi
  if grep -qiE 'oom[-_ ]kill|out of memory|cannot allocate memory|MemoryError' "$log" 2>/dev/null; then
    hints+="- **It ran out of memory.** Launch it again with more *Memory*."$'\n'
  fi
  if grep -qiE 'permission denied|not writable|read-only file system' "$log" 2>/dev/null; then
    hints+="- **A file or folder could not be written** (\"permission denied\"). Check the paths in the log below; if they are in your home or project folder, send the session ID to the HPC team."$'\n'
  fi
  [[ -n "$hints" ]] || hints="- No known cause was detected; the log lines below usually show what failed."$'\n'
  lines="$(grep -aiE 'error|denied|no space|quota|bus error|segmentation|killed|fatal|cannot|failed|not found|traceback|exception' "$log" 2>/dev/null \
           | grep -avE '^\+ |^ERROR: .* (did not start|exited)' | tail -n 12 | cut -c1-300)"
  ood_report "$name did not start" \
"$name $( [[ "$exited" == 1 ]] && echo "stopped after ${secs} s" || echo "did not answer within ${secs} s"), so the session was ended.

**Most likely cause:**
${hints}
**Last error lines from the log** (full log: *output.log* in the session folder):
\`\`\`
${lines:-(no error lines found)}
\`\`\`"
}

# ood_wait_for_port NAME HOST PORT [SECONDS] - like wait_until_port_used, but
# stops as soon as the server process exits and explains the failure.
ood_wait_for_port() {
  local name="$1" host="$2" port="$3" timeout="${4:-120}" i exited=0
  echo "Waiting for $name to open port ${port}..."
  for (( i = 0; i < timeout; i++ )); do
    if port_used "${host}:${port}"; then
      echo "Discovered $name listening on port ${port}!"
      return 0
    fi
    [[ -s "$OOD_ERROR_FILE" ]] && return 1          # a check already explained it
    if [[ -n "${SCRIPT_PID:-}" ]] && ! kill -0 "$SCRIPT_PID" 2>/dev/null; then
      exited=1; break
    fi
    sleep 1
  done
  sleep 1   # let the last log lines land
  _ood_startup_report "$name" "$i" "$exited"
  return 1
}
