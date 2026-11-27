#!/usr/bin/env python3
"""Which of the cluster's modules work inside each app image.

Run as a Slurm job on a node that has the cluster's modules and singularity
(see README.md; the release bot runs it weekly and when an image is added):

  host_modules.py OUTDIR IMAGE.sif [IMAGE.sif ...]

1. For every module in MODULE_ROOT (EasyBuild, the same on all nodes) except
   compilers/toolchains, MPI, CUDA, interpreters and the like, load it in a
   clean login shell and record what it sets: OUTDIR/env/<Name>/<version>.env
   (lines "PREPEND VAR=value" / "SET VAR=value"), and its own executables (in
   its EBROOT, not its dependencies').
2. In each image, with /opt/software and /opt/ohpc bound and the module's
   library path, check those executables: binaries must find all their
   libraries; scripts must have an interpreter that does.
3. OUTDIR/<image>.tsv lists the modules that pass: name/version, default
   (the highest passing version of the name), and the commands it provides.
   OUTDIR/<image>.rejected.tsv lists the others and why.

Inside the containers, `module load NAME` (ood_slurm.sh) then makes small
wrappers for those commands that apply the module's settings to that one
command only, so the container's own Python/R never see its libraries.
"""
import concurrent.futures as cf
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

MODULE_ROOT = Path(os.environ.get("OOD_HOST_MODULE_ROOT", "/opt/software/modules/all"))
# Modules that would replace or break the images' own tools, or need host
# fabric/drivers: never offered inside the containers.
EXCLUDE = re.compile(r"^(GCC|GCCcore|foss|fosscuda|gompi|gompic|gfbf|iimpi|iompi|intel|intel-compilers|iccifort|"
                     r"OpenMPI|MPICH|MVAPICH2|impi|UCX|UCC|libfabric|PMIx|hwloc|OpenSSL|"
                     r"CUDA|CUDAcore|cuDNN|NCCL|cuTENSOR|"
                     r"Python|Python-bundle-PyPI|SciPy-bundle|R|R-bundle-.*|Perl|Perl-bundle-.*|Java|Ruby|Julia|Go|Rust|Lua|Tcl|Tk|"
                     r"Miniconda3|Anaconda3|Mamba|Miniforge3|singularity|apptainer|Singularity|slurm|"
                     r"binutils|Autoconf|Automake|Autotools|libtool|M4|make|CMake|Ninja|Meson|pkgconf|pkg-config|Bison|flex|"
                     r"help2man|texinfo|Doxygen|git|EasyBuild|Lmod)$", re.I)
# Command names a module may not take over inside the containers.
PROTECTED = {"python", "python3", "pip", "pip3", "conda", "mamba", "jupyter", "ipython", "R", "Rscript", "perl", "java",
             "bash", "sh", "env", "sbatch", "srun", "salloc", "squeue", "scancel", "sinfo", "sacct", "scontrol",
             "module", "ml", "ood-host", "ood-run", "ood-shell", "singularity", "apptainer"}
PATHLIKE = {"PATH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "CPATH", "MANPATH", "PKG_CONFIG_PATH", "PYTHONPATH", "PERL5LIB",
            "XDG_DATA_DIRS", "ACLOCAL_PATH", "CMAKE_PREFIX_PATH", "CLASSPATH", "R_LIBS_SITE", "JULIA_DEPOT_PATH"}
SKIP = re.compile(r"^(LMOD|_LM|__LM|LOADEDMODULES|MODULEPATH|MODULESHOME|BASH_FUNC_|_$|SHLVL|PWD|OLDPWD)")


def modules():
    out = []
    for f in sorted(MODULE_ROOT.glob("*/*.lua")):
        name, ver = f.parent.name, f.name[:-4]
        if not EXCLUDE.match(name):
            out.append(f"{name}/{ver}")
    return out


def environ_after(mod):
    script = ('. /etc/profile >/dev/null 2>&1; module purge >/dev/null 2>&1; env -0; printf "\\0#OOD#\\0"; '
              f'module load {mod} >/dev/null 2>&1 && env -0')
    user = os.environ.get("USER", "")
    p = subprocess.run(["env", "-i", f"HOME={os.environ['HOME']}", f"USER={user}", "PATH=/usr/bin:/bin", "TERM=dumb",
                        "bash", "--noprofile", "--norc", "-c", script], capture_output=True, timeout=120)
    before, _, after = p.stdout.partition(b"\0#OOD#\0")
    parse = lambda b: dict(x.split("=", 1) for x in b.decode("utf-8", "replace").split("\0") if "=" in x)
    return parse(before), parse(after) if after else None


def own_executables(mod, env):
    name = mod.split("/")[0]
    key = "EBROOT" + re.sub(r"[^A-Z0-9]", "", name.upper().replace("-", "MIN"))
    root = env.get(key) or next((v for k, v in env.items() if k.startswith("EBROOT") and
                                 re.sub(r"[^A-Z0-9]", "", k[6:]) == re.sub(r"[^A-Z0-9]", "", name.upper())), None)
    if not root:
        return None, []
    exes = []
    for b in ("bin",):
        d = Path(root) / b
        if d.is_dir():
            for f in sorted(d.iterdir()):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_mode & 0o111 and f.name not in PROTECTED:
                    exes.append(str(f))
    return root, exes


def capture(mod, outdir):
    try:
        before, after = environ_after(mod)
    except subprocess.TimeoutExpired:
        return mod, "load timed out", None
    if after is None:
        return mod, "does not load", None
    lines = []
    for k, v in sorted(after.items()):
        if SKIP.match(k) or before.get(k) == v:
            continue
        if k in PATHLIKE:
            old = [x for x in before.get(k, "").split(":") if x]
            new = [x for x in v.split(":") if x and x not in old]
            if new:
                lines.append(f"PREPEND {k}={':'.join(new)}")
        elif "\n" not in v:
            lines.append(f"SET {k}={v}")
    root, exes = own_executables(mod, after)
    if not exes:
        return mod, "no commands of its own", None
    p = outdir / "env" / f"{mod}.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    get = lambda var: next((l.split("=", 1)[1] for l in lines if l.startswith(f"PREPEND {var}=")), "")
    return mod, "ok", (get("LD_LIBRARY_PATH"), get("PATH"), exes)


# Inside the image: for each "module<TAB>ld<TAB>exe exe ..." line, print
# "module<TAB>ok" or "module<TAB>broken<TAB>reason".
CHECK = r'''
while IFS=$'\t' read -r mod ld mpath exes; do
  bad=""
  for e in $exes; do
    hdr=$(head -c 4 "$e" 2>/dev/null | od -An -c | tr -d ' ')
    if [[ "$hdr" == "177ELF" ]]; then
      m=$(LD_LIBRARY_PATH="$ld" ldd "$e" 2>&1 | grep -E "not found" | head -1)
      [[ -n "$m" ]] && { bad="$(basename "$e"): $(echo $m)"; break; }
    elif [[ "$(head -c 2 "$e" 2>/dev/null)" == "#!" ]]; then
      read -r i a _ <<< "$(head -1 "$e" | sed -E 's/^#! *//')"
      [[ "$i" == */env ]] && i="$(PATH="$mpath:$PATH" command -v "$a" 2>/dev/null)"
      case "$i" in
        /opt/*) m=$(LD_LIBRARY_PATH="$ld" ldd "$i" 2>&1 | grep -E "not found" | head -1)
                [[ -n "$m" ]] && { bad="$(basename "$e") (interpreter $i): $(echo $m)"; break; } ;;
        */sh|*/bash|*/dash|*/ksh|*/zsh|*/csh|*/tcsh) ;;
        "") bad="$(basename "$e"): interpreter not found"; break ;;
        *python*|*perl*|*Rscript*|*/R|*ruby*|*java*) bad="$(basename "$e"): would run the container's $i"; break ;;
      esac
    fi
  done
  if [[ -n "$bad" ]]; then printf '%s\tbroken\t%s\n' "$mod" "$bad"; else printf '%s\tok\n' "$mod"; fi
done
'''


def check_image(image, captured, outdir):
    rows = "".join(f"{m}\t{ld}\t{mp}\t{' '.join(exes)}\n" for m, (ld, mp, exes) in captured.items())
    # A module's own interpreters (e.g. a script's "#!/usr/bin/env python3") come from its PATH additions.
    p = subprocess.run(["singularity", "exec", "--cleanenv", "-B", "/opt/software", "-B", "/opt/ohpc", image,
                        "bash", "-c", CHECK], input=rows, capture_output=True, text=True, timeout=7200)
    result = dict(l.split("\t", 1) for l in p.stdout.splitlines() if "\t" in l)
    ok = sorted(m for m, r in result.items() if r == "ok")
    best = {}
    for m in ok:
        n, v = m.split("/", 1)
        if n not in best or vkey(v) > vkey(best[n]):
            best[n] = v
    base = Path(image).stem
    with open(outdir / f"{base}.tsv", "w") as f:
        f.write("# module\tdefault\tcommands (generated by .github/ood-modules/host_modules.py)\n")
        for m in ok:
            n, v = m.split("/", 1)
            f.write(f"{m}\t{'default' if best[n] == v else ''}\t{' '.join(os.path.basename(e) for e in captured[m][2])}\n")
    with open(outdir / f"{base}.rejected.tsv", "w") as f:
        for m, r in sorted(result.items()):
            if r != "ok":
                f.write(f"{m}\t{r}\n")
    return base, len(ok), len(result) - len(ok)


def vkey(v):
    return [int(x) if x.isdigit() else x for x in re.split(r"[.\-_]", v)]


def main():
    outdir, images = Path(sys.argv[1]), sys.argv[2:]
    outdir.mkdir(parents=True, exist_ok=True)
    mods = modules()
    print(f"{len(mods)} candidate modules in {MODULE_ROOT}", flush=True)
    captured, skipped = {}, {}
    with cf.ThreadPoolExecutor(int(os.environ.get("SLURM_CPUS_PER_TASK", "4")) * 2) as ex:
        for mod, status, data in ex.map(lambda m: capture(m, outdir), mods):
            (captured.__setitem__(mod, data) if status == "ok" else skipped.__setitem__(mod, status))
    print(f"captured {len(captured)}, skipped {len(skipped)} "
          f"({', '.join(f'{s}: {sum(1 for x in skipped.values() if x == s)}' for s in sorted(set(skipped.values())))})", flush=True)
    with open(outdir / "skipped.tsv", "w") as f:
        f.writelines(f"{m}\t{s}\n" for m, s in sorted(skipped.items()))
    for image in images:
        base, n_ok, n_bad = check_image(image, captured, outdir)
        print(f"{base}: {n_ok} modules usable, {n_bad} rejected", flush=True)


if __name__ == "__main__":
    main()
