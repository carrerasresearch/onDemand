# ood_slurm.sh - the cluster's Slurm commands inside an app's container.
#
# Master copy: .github/ood-checks/ood_slurm.sh. The apps with a terminal
# (jupyter, rstudio_bioconductor_321, vscode_cli_tunnel, igv) have an
# identical copy in template/; run .github/ood-checks/sync.sh after editing.
#
# Run on the host, before the container starts:
#
#   source ./ood_slurm.sh
#   ood_slurm_setup IMAGE DIR    # DIR: visible at the same path in the container
#   singularity exec -B "$OOD_SLURM_BIND" ...   # and put $OOD_SLURM_PATH first on PATH
#
# Inside the container, sbatch, srun, squeue, ... are wrappers that run the
# cluster's own client (whatever version the node uses: found here, never
# hard-coded) with the node's login environment. So jobs submitted from an
# app terminal start like jobs submitted from a cluster shell: they do not
# inherit the container's PATH, LD_LIBRARY_PATH or the session job's SLURM_*
# variables, and `module` works in their scripts. `ood-host` runs a command
# (or a shell) on this node outside the container, within the session's own
# allocation. Nothing here may stop an app from starting: on any problem it
# prints a warning and leaves OOD_SLURM_BIND/OOD_SLURM_PATH empty.
#
#   ood_slurm_selftest IMAGE     # prints "ood-slurm: ok ..." or "ood-slurm: FAILED ..."
#
# Do not use `set -e` here: this file is sourced into the apps' job scripts.

OOD_SLURM_BIND=""
OOD_SLURM_PATH=""
OOD_SLURM_VERSION=""

_ood_slurm_warn() { echo "WARNING (ood-slurm): $*" >&2; }

# The node's login environment (system profile only, not the user's dot files,
# which may start agents or print). NUL-separated, so exported shell functions
# (module) survive.
_ood_slurm_login_env() {
  local home user
  user="$(id -un)"
  home="$(getent passwd "$user" | cut -d: -f6)"
  env -i HOME="${home:-$HOME}" USER="$user" LOGNAME="$user" SHELL=/bin/bash TERM=dumb \
      LANG="${LANG:-C.UTF-8}" PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin \
      bash --noprofile --norc -c '
        . /etc/profile >/dev/null 2>&1
        env -0' 2>/dev/null
}

# _ood_slurm_get FILE VAR - a variable from a NUL-separated environment file.
_ood_slurm_get() {
  tr '\0' '\n' < "$1" | sed -n "s/^$2=//p" | head -1
}

ood_slurm_setup() {
  local image="$1" dir="${2:-$PWD}/ood-slurm"
  OOD_SLURM_BIND=""; OOD_SLURM_PATH=""; OOD_SLURM_VERSION=""
  command -v singularity >/dev/null 2>&1 || { _ood_slurm_warn "singularity not found; no Slurm in the app"; return 0; }
  rm -rf "$dir" && mkdir -p "$dir/bin" "$dir/lib" || { _ood_slurm_warn "cannot create $dir"; return 0; }

  _ood_slurm_login_env > "$dir/env0"
  # The client: the install this node's slurmd runs from (by definition the
  # cluster's version; follows upgrades), else sbatch on the login PATH, else
  # /opt/slurm/latest. The config: slurmd's -f, else the job's SLURM_CONF.
  local d_args d_bin prefix="" conf="" bindir sbatch
  d_args="$(ps -C slurmd -o args= 2>/dev/null | head -1)"
  d_bin="${d_args%% *}"
  if [[ "$d_bin" == */sbin/slurmd && -x "${d_bin%/sbin/slurmd}/bin/sbatch" ]]; then
    prefix="${d_bin%/sbin/slurmd}"
    [[ " $d_args " =~ \ -f\ ([^ ]+) ]] && conf="${BASH_REMATCH[1]}"
  fi
  if [[ -z "$prefix" ]]; then
    sbatch="$(env PATH="$(_ood_slurm_get "$dir/env0" PATH)" sh -c 'command -v sbatch')"
    [[ -z "$sbatch" && -x /opt/slurm/latest/bin/sbatch ]] && sbatch=/opt/slurm/latest/bin/sbatch
    [[ -n "$sbatch" ]] && prefix="$(dirname "$(dirname "$(readlink -f "$sbatch")")")"
  fi
  [[ -n "$prefix" ]] || { _ood_slurm_warn "no Slurm client found on $(hostname)"; rm -rf "$dir"; return 0; }
  prefix="$(readlink -f "$prefix")"
  bindir="$prefix/bin"
  [[ -n "$conf" ]] || conf="${SLURM_CONF:-}"
  [[ -n "$conf" && -f "$conf" ]] || conf="$(_ood_slurm_get "$dir/env0" SLURM_CONF)"
  [[ -n "$conf" && -f "$conf" ]] || conf="$prefix/etc/slurm.conf"
  [[ -f "$conf" ]] || { _ood_slurm_warn "Slurm config $conf not found"; rm -rf "$dir"; return 0; }
  local plugindir
  plugindir="$(sed -n -E 's/^[[:space:]]*[Pp][Ll][Uu][Gg][Ii][Nn][Dd][Ii][Rr][[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p' "$conf" | head -1)"

  # Library path for the Slurm commands only: the client's own libraries, its
  # plugins, and libmunge if the image lacks a usable one.
  local libs="" p
  for p in "$prefix/lib" "$prefix/lib64" "$prefix/lib/slurm" "$prefix/lib64/slurm" "$plugindir" \
           $(_ood_slurm_get "$dir/env0" LD_LIBRARY_PATH | tr ':' '\n' | grep -i -E 'slurm|munge'); do
    [[ -d "$p" && ":$libs:" != *":$p:"* ]] && libs="${libs:+$libs:}$p"
  done

  local joblibs="" l2
  for l2 in $(tr ':' ' ' <<< "$libs") $(_ood_slurm_get "$dir/env0" LD_LIBRARY_PATH | tr ':' ' '); do
    [[ -d "$l2" && ":$joblibs:" != *":$l2:"* ]] && joblibs="${joblibs:+$joblibs:}$l2"
  done
  # Jobs get the login environment with this client first on PATH, so sbatch
  # also works inside them (on some nodes the login profile fails to load it).
  local envs=() e
  mapfile -d '' envs < "$dir/env0"
  local lp_old ll_old
  lp_old="$(_ood_slurm_get "$dir/env0" PATH)"; ll_old="$(_ood_slurm_get "$dir/env0" LD_LIBRARY_PATH)"
  {
    for e in "${envs[@]}"; do
      case "$e" in PATH=*|LD_LIBRARY_PATH=*|SLURM_CONF=*) ;; *) printf '%s\0' "$e" ;; esac
    done
    local np="$bindir" l
    for l in $(tr ':' ' ' <<< "$lp_old"); do [[ ":$np:" != *":$l:"* ]] && np="$np:$l"; done
    printf '%s\0' "PATH=$np" "SLURM_CONF=$conf"
    printf '%s\0' "LD_LIBRARY_PATH=$joblibs"
  } > "$dir/env0.new" && mv "$dir/env0.new" "$dir/env0"

  # What the image has: users, groups, libmunge, glibc.
  local info
  info="$(singularity exec "$image" sh -c 'cat /etc/passwd; echo "#OOD#"; cat /etc/group; echo "#OOD#";
           (ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null) | grep -c "libmunge\.so\.2 "; echo "#OOD#";
           ldd --version 2>&1 | head -1 | grep -o "[0-9]\+\.[0-9]\+$"' 2>/dev/null)" \
    || { _ood_slurm_warn "cannot read $image"; rm -rf "$dir"; return 0; }
  _ood_part() { awk -v k="$1" '/^#OOD#$/ {n++; next} n == k' <<< "$info"; }
  _ood_part 0 > "$dir/passwd"
  _ood_part 1 > "$dir/group"
  local has_munge glibc
  has_munge="$(_ood_part 2 | tr -dc '0-9')"
  glibc="$(_ood_part 3 | tr -dc '0-9.')"
  # The cluster's client may be built for a newer glibc than an old image has
  # (e.g. Ubuntu 24.04 builds in an Ubuntu 20.04 image): then say so instead
  # of failing with loader errors.
  local client_glibc too_old=""
  client_glibc="$(cat "$bindir/sbatch" $(ls "$prefix"/lib*/libslurmfull*.so "$prefix"/lib*/slurm/libslurmfull*.so 2>/dev/null) 2>/dev/null \
                  | grep -a -o 'GLIBC_2\.[0-9]*' | sed 's/GLIBC_//' | sort -t. -k2 -n | tail -1)"
  if [[ -n "$client_glibc" && -n "$glibc" ]] && [[ "$(printf '%s\n%s\n' "$client_glibc" "$glibc" | sort -t. -k1,1n -k2,2n | head -1)" != "$client_glibc" ]]; then
    too_old="this image has glibc $glibc; the cluster's Slurm $(basename "$prefix") needs $client_glibc"
  fi

  # The client checks that SlurmUser exists; the user and their groups so
  # that names resolve as on the node.
  local slurmuser
  slurmuser="$(sed -n -E 's/^[[:space:]]*SlurmUser[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/Ip' "$conf" | head -1)"
  for p in "${slurmuser:-slurm}" "$(id -un)"; do
    grep -q "^$p:" "$dir/passwd" || getent passwd "$p" >> "$dir/passwd"
  done
  for p in "${slurmuser:-slurm}" $(id -G); do
    getent group "$p" | while IFS=: read -r g _ gid _; do
      grep -q "^$g:" "$dir/group" || grep -q "^[^:]*:[^:]*:$gid:" "$dir/group" || getent group "$gid" >> "$dir/group"
    done
  done
  if [[ "${has_munge:-0}" == 0 ]]; then
    local m need
    m="$( (ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null) | awk '/libmunge\.so\.2 .*x86-64/ {print $NF; exit}')"
    need="$( [[ -n "$m" ]] && grep -a -o 'GLIBC_2\.[0-9]*' "$m" 2>/dev/null | sort -t. -k2 -n | tail -1 | sed 's/GLIBC_//')"
    if [[ -n "$m" && -n "$glibc" && -n "$need" ]] && [[ "$(printf '%s\n%s\n' "$need" "$glibc" | sort -t. -k1,1n -k2,2n | head -1)" == "$need" ]]; then
      cp -L "$m" "$dir/lib/libmunge.so.2"
    elif [[ -f "${OOD_SLURM_OLD_MUNGE:-/ijc/PUBLIC/DATA/images/ood/lib/glibc2.17/libmunge.so.2}" ]]; then
      cp -L "${OOD_SLURM_OLD_MUNGE:-/ijc/PUBLIC/DATA/images/ood/lib/glibc2.17/libmunge.so.2}" "$dir/lib/libmunge.so.2"
    else
      _ood_slurm_warn "no libmunge usable in $image (glibc $glibc); Slurm commands will fail there"
    fi
  fi

  # Binds: wherever the client, its plugins, libraries and config live, and
  # munge's socket; the passwd/group above over the image's.
  local cands=() binds=() b skip
  for p in /opt/slurm /opt/software/slurm /etc/slurm "$prefix" "$plugindir" $(tr ':' ' ' <<< "$libs") "$(dirname "$conf")" \
           "$(dirname "$(sed -n -E 's/^[[:space:]]*AuthInfo[[:space:]]*=.*socket=([^,[:space:]]+).*/\1/Ip' "$conf" | head -1)")" \
           /run/munge /var/run/munge; do
    [[ -n "$p" && "$p" != "." && -e "$p" ]] || continue
    p="$(readlink -f "$p")"
    [[ "$p" == "$dir" || "$p" == "$dir/"* ]] && continue
    cands+=("$p")
  done
  # Parents first; drop paths inside another bind.
  while IFS= read -r p; do
    skip=""
    for b in "${binds[@]}"; do [[ "$p" == "$b" || "$p" == "$b/"* ]] && skip=1; done
    [[ -z "$skip" ]] && binds+=("$p")
  done < <(printf '%s\n' "${cands[@]}" | awk '{print length($0) "\t" $0}' | sort -n | cut -f2-)
  OOD_SLURM_BIND="$(IFS=,; echo "${binds[*]}"),$dir/passwd:/etc/passwd,$dir/group:/etc/group"
  # A libmunge for the image goes where its loader looks anyway, so the
  # commands (and the jobs they submit) need no extra library path.
  [[ -f "$dir/lib/libmunge.so.2" ]] && OOD_SLURM_BIND="$OOD_SLURM_BIND,$dir/lib/libmunge.so.2:/usr/lib/x86_64-linux-gnu/libmunge.so.2"

  # The wrappers. User-set SBATCH_*/SRUN_*/... options are kept, except the
  # *_EXPORT defaults some images set (the jobs get the login environment).
  local cmd
  for cmd in sbatch srun salloc squeue scancel sinfo sacct scontrol sstat sprio sshare sattach sbcast sdiag; do
    [[ -x "$bindir/$cmd" ]] || continue
    if [[ -n "$too_old" ]]; then
      printf '#!/bin/sh\necho "%s: not available in this app image (%s). Use a newer image, or run it from a cluster shell." >&2\nexit 1\n' \
        "$cmd" "$too_old" > "$dir/bin/$cmd"
      chmod 755 "$dir/bin/$cmd"; continue
    fi
    cat > "$dir/bin/$cmd" <<EOF
#!/bin/bash
# Generated by ood_slurm.sh: the cluster's $cmd ($bindir) with the node's login environment.
mapfile -d '' _ood_env < "$dir/env0"
_ood_keep=()
for _v in \$(compgen -v | grep -E '^(SBATCH|SALLOC|SRUN|SQUEUE|SINFO|SACCT|SCONTROL|SPRIO|SSHARE|SSTAT)_' | grep -v -E '^(SBATCH_EXPORT|SALLOC_EXPORT|SRUN_EXPORT_ENV|SBATCH_GET_USER_ENV)\$'); do
  _ood_keep+=("\$_v=\${!_v}")
done
exec env -i "\${_ood_env[@]}" "\${_ood_keep[@]}" "$bindir/$cmd" "\$@"
EOF
    chmod 755 "$dir/bin/$cmd"
  done

  # ood-host: a command (default: a login shell) on this node, outside the
  # container, in the session's own job (shares its CPUs and memory).
  cat > "$dir/bin/ood-host" <<EOF
#!/bin/bash
# Generated by ood_slurm.sh. Usage: ood-host [COMMAND ...]
# Runs COMMAND (default: an interactive login shell) on $(hostname), outside the
# app's container, inside this session's job ${SLURM_JOB_ID:-?}: host tools and
# \`module load\` work there as in a cluster shell.
if [[ \$# -eq 0 ]]; then set -- bash -l; pty=(--pty); else pty=(); [[ -t 0 && -t 1 ]] && pty=(--pty); fi
exec "$dir/bin/srun" --jobid="${SLURM_JOB_ID:-}" --overlap -N1 -n1 --chdir="\$PWD" "\${pty[@]}" "\$@"
EOF
  chmod 755 "$dir/bin/ood-host"
  # ood-run / ood-shell (module load ood/...) with this session's image as the
  # default module, when that image has one.
  local root="${OOD_ROOT:-/ijc/PUBLIC/DATA/images/ood}" mod=""
  [[ -f "$root/catalog.tsv" ]] && mod="$(awk -F'\t' -v img="$image" '$3 == img {print $1; exit}' "$root/catalog.tsv")"
  for cmd in ood-run ood-shell; do
    [[ -x "$root/bin/$cmd" ]] || continue
    printf '#!/bin/sh\n# Generated by ood_slurm.sh: %s for this session (default module: %s).\nOOD_ROOT=%q OOD_MODULE="${OOD_MODULE:-%s}" OOD_GPU_PARTITION="${OOD_GPU_PARTITION:-test-gpu}" exec %q "$@"\n' \
      "$cmd" "${mod:-none}" "$root" "$mod" "$root/bin/$cmd" > "$dir/bin/$cmd"
    chmod 755 "$dir/bin/$cmd"
  done
  # `module` in the container: the cluster's modules that were tested in this
  # image (.github/ood-modules/host_modules.py) load as per-command wrappers
  # that apply the module's settings to that command only, so they cannot
  # break the image's own Python/R. Others are refused with the alternatives.
  local hm="$root/host-modules" list=""
  list="$hm/$(basename "$image" .sif).tsv"
  if [[ -f "$list" ]]; then
    OOD_SLURM_BIND="$OOD_SLURM_BIND,/opt/software"
    [[ -d /opt/ohpc ]] && OOD_SLURM_BIND="$OOD_SLURM_BIND,/opt/ohpc"
    sed -e "s#@LIST@#$list#g" -e "s#@ENVDIR@#$hm/env#g" -e "s#@BIN@#$dir/bin#g" -e "s#@STATE@#$dir/modules.loaded#g" \
      > "$dir/bin/module" <<'OODMOD'
#!/bin/bash
# `module` inside an OnDemand app's container (generated by ood_slurm.sh).
LIST=@LIST@ ENVDIR=@ENVDIR@ BIN=@BIN@ STATE=@STATE@
touch "$STATE"
others() {
  cat >&2 <<'MSG'
Modules that are not in `module avail` here could break this app's own
Python/R, so they are not offered inside the container. Use them on the host:
  ood-host bash -lc 'module load NAME && ...'    # this node, this session's job
  sbatch / srun ...                              # a new job; `module load` works there
For Python/R packages, use a conda environment (see the app's form help).
MSG
}
resolve() { # NAME or NAME/VERSION -> NAME/VERSION from the list
  local hit
  if [[ "$1" == */* ]]; then hit=$(awk -F'\t' -v m="$1" 'tolower($1) == tolower(m) {print $1; exit}' "$LIST")
  else hit=$(awk -F'\t' -v n="$1" '{split($1, a, "/")} tolower(a[1]) == tolower(n) && $2 == "default" {print $1; exit}' "$LIST"); fi
  [[ -n "$hit" ]] && echo "$hit"
}
unload1() {
  local m="$1" f
  for f in "$BIN"/*; do grep -q -x "# ood-module $m" "$f" 2>/dev/null && rm -f "$f"; done
  grep -v -x -F "$m" "$STATE" > "$STATE.new"; mv "$STATE.new" "$STATE"
}
load1() {
  local m n env roots c path made=() skipped=()
  m=$(resolve "$1") || true
  if [[ -z "$m" ]]; then echo "module: '$1' is not available inside this app's container." >&2; others; return 1; fi
  n=${m%%/*}
  while read -r old; do [[ "${old%%/*}" == "$n" && "$old" != "$m" ]] && unload1 "$old"; done < "$STATE"
  env="$ENVDIR/$m.env"
  roots=$(sed -n 's/^SET EBROOT[^=]*=//p' "$env")
  for c in $(awk -F'\t' -v m="$m" '$1 == m {print $3}' "$LIST"); do
    path=""; for r in $roots; do [[ -x "$r/bin/$c" ]] && path="$r/bin/$c" && break; done
    [[ -n "$path" ]] || continue
    if [[ -e "$BIN/$c" ]] && ! grep -q '^# ood-module ' "$BIN/$c"; then skipped+=("$c"); continue; fi
    cat > "$BIN/$c" <<SHIM
#!/bin/bash
# ood-module $m
# $c with the settings of module $m, for this command only (generated by \`module load\`).
while IFS= read -r l; do
  case "\$l" in
    "PREPEND "*) v=\${l#PREPEND }; k=\${v%%=*}; export "\$k=\${v#*=}\${!k:+:\${!k}}" ;;
    "SET "*) v=\${l#SET }; export "\${v%%=*}=\${v#*=}" ;;
  esac
done < "$env"
exec "$path" "\$@"
SHIM
    chmod 755 "$BIN/$c"; made+=("$c")
  done
  grep -q -x -F "$m" "$STATE" || echo "$m" >> "$STATE"
  echo "Loaded $m: ${made[*]:-no commands}" >&2
  [[ ${#skipped[@]} -gt 0 ]] && echo "  (not replaced, the app's own: ${skipped[*]})" >&2
  return 0
}
cmd="${1:-help}"; shift || true
case "$cmd" in
  avail|av|spider)
    echo "Cluster modules tested in this app's image (D = default; \`module load NAME\`):"
    awk -F'\t' -v p="${1:-}" '!/^#/ && tolower($1) ~ tolower(p) {printf "  %s%s\n", $1, ($2 == "default" ? " (D)" : "")}' "$LIST" ;;
  load|add) rc=0; for a in "$@"; do load1 "$a" || rc=1; done
            [[ $rc == 0 ]] && echo "(each command runs with its module's settings; this shell is unchanged; run \`hash -r\` if needed)" >&2
            exit $rc ;;
  unload|rm|del) for a in "$@"; do m=$(grep -i -m1 -E "^$a(/|$)" "$STATE"); [[ -n "$m" ]] && unload1 "$m"; done ;;
  purge) while read -r m; do unload1 "$m"; done < "$STATE" ;;
  list) if [[ -s "$STATE" ]]; then echo "Loaded (inside this app):"; sed 's/^/  /' "$STATE"; else echo "No modules loaded."; fi ;;
  show|display|help|whatis)
    if [[ -z "${1:-}" ]]; then echo "module avail [TEXT] | load NAME[/VERSION] ... | unload NAME | list | purge | show NAME"; others; exit 0; fi
    m=$(resolve "$1"); [[ -n "$m" ]] || { echo "module: '$1' is not available inside this app's container." >&2; others; exit 1; }
    echo "$m - commands: $(awk -F'\t' -v m="$m" '$1 == m {print $3}' "$LIST")"; sed 's/^/  /' "$ENVDIR/$m.env" ;;
  *) echo "module $cmd: not supported inside this app's container." >&2; others; exit 1 ;;
esac
OODMOD
  else
    cat > "$dir/bin/module" <<'OODMOD'
#!/bin/bash
cat >&2 <<'MSG'
The cluster's modules are not loaded inside this app's container: their
libraries could break the app's own Python/R. Use them on the host instead:

  ood-host                       # a shell on this node, in this session's job
  ood-host bash -lc 'module load BCFtools && bcftools --version'
  sbatch / srun ...              # a new job, where `module load` works as usual
  ood-run -c 8 -- python x.py    # a new job in this app's image (module load ood/...)

For Python/R packages, use a conda environment (see the app's form help).
MSG
exit 1
OODMOD
  fi
  chmod 755 "$dir/bin/module"
  printf '#!/bin/sh\nexec "$(dirname "$0")/module" "$@"\n' > "$dir/bin/ml"; chmod 755 "$dir/bin/ml"

  OOD_SLURM_PATH="$dir/bin"
  if [[ -n "$too_old" ]]; then
    _ood_slurm_warn "Slurm commands disabled in this image: $too_old"
    export OOD_SLURM_BIND OOD_SLURM_PATH OOD_SLURM_VERSION
    return 0
  fi
  OOD_SLURM_VERSION="$("$dir/bin/sbatch" --version 2>/dev/null | awk '{print $2}')"
  local ctl
  ctl="$("$dir/bin/scontrol" show config 2>/dev/null | awk '/^SLURM_VERSION/ {print $3}')"
  if [[ -n "$ctl" && -n "$OOD_SLURM_VERSION" && "$ctl" != "$OOD_SLURM_VERSION" ]]; then
    _ood_slurm_warn "client $OOD_SLURM_VERSION, controller $ctl"
  fi
  echo "ood-slurm: Slurm ${OOD_SLURM_VERSION:-?} from $bindir for commands in the container"
  export OOD_SLURM_BIND OOD_SLURM_PATH OOD_SLURM_VERSION
  return 0
}

# ood_slurm_selftest IMAGE - after ood_slurm_setup: run the wrappers in the image.
ood_slurm_selftest() {
  local image="$1" out
  if [[ -z "$OOD_SLURM_PATH" ]]; then echo "ood-slurm: FAILED (not set up)"; return 1; fi
  if [[ -z "$OOD_SLURM_VERSION" ]] && grep -q "not available in this app image" "$OOD_SLURM_PATH/sbatch" 2>/dev/null; then
    echo "ood-slurm: disabled (image too old for the cluster's Slurm)"; return 0
  fi
  out="$(singularity exec --cleanenv -B "$OOD_SLURM_BIND" --env PATH="$OOD_SLURM_PATH:/usr/local/bin:/usr/bin:/bin" "$image" bash -c '
    v=$(sbatch --version 2>&1); q=$(squeue -h -j "'"${SLURM_JOB_ID:-0}"'" -o %i 2>&1)
    t=$(printf "#!/bin/bash\ntrue\n" | sbatch --test-only -t 1 2>&1 | tail -1)
    echo "$v | squeue: $q | test-only: $t"' 2>&1)"
  if [[ "$out" == slurm\ * && "$out" == *"to start at"* && "$out" != *rror* ]]; then
    echo "ood-slurm: ok $out"
  else
    echo "ood-slurm: FAILED $out"; return 1
  fi
}
