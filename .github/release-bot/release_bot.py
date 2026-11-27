#!/usr/bin/env python3
"""Release bot for the OnDemand apps in this repo.

Runs daily from a systemd user timer on a maintainer workstation (README.md).
For each app plugin it checks whether upstream published a version the app
does not offer yet. For each new version it:

  1. builds the image from the app's .def and uploads it to
     /ijc/PUBLIC/DATA/images (never overwriting a file);
  2. adds the version to the app on a branch off master (form defaults are
     never changed) and pushes it;
  3. publishes the branch to the ood-poc portal and checks that the portal
     serves it;
  4. renders the job script exactly as ood-poc serves it (cluster and
     partition rewritten for the POC cluster) and runs it as a Slurm job on
     the POC cluster, probing the running app;
  5. if that passes, opens a pull request to master and emails the
     maintainer, who reviews and merges. Merging publishes to production.

Any failure is emailed with the log; nothing reaches production without a
merge.

  release_bot.py [--dry-run] [--only APP]
  release_bot.py --test-existing APP:VERSION [--ref BRANCH]
      run steps 3-4 for a version the app already offers on BRANCH (default
      master): checks the POC path end to end, changes nothing else.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = Path.home() / ".config/ood-release-bot/env"
STATE = Path.home() / ".cache/ood-release-bot"
REPO_SLUG = "carrerasresearch/onDemand"
IMAGES = "/ijc/PUBLIC/DATA/images"
RUN_ID = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
LOG = STATE / "logs" / f"{RUN_ID}.log"

cfg = {
    "SSH_HOST": "minastirith",           # cluster login: uploads, mail
    "POC_HOST": "hpclogin-new",          # POC cluster login, reached via SSH_HOST
    "POC_PARTITION": "hpc-new",
    "POC_SLURM_BIN": "/opt/slurm/20.11.9/bin",
    "KUBE_HOST": "super@maverick",       # kubectl for the ood-poc namespace
    "POC_DEPLOY_REPO": "carrerasresearch/OnDemandDocker",
    "POC_DEPLOY_REF": "poc-new-cluster",
}


# --------------------------------------------------------------------------- helpers
def log(msg):
    line = f"[{dt.datetime.now():%F %T}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd, *, cwd=None, check=True, input=None, timeout=None):
    """Run a command (list or shell string); return stdout. Logs stderr on failure."""
    shell = isinstance(cmd, str)
    p = subprocess.run(cmd, shell=shell, cwd=cwd, input=input, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"command failed ({p.returncode}): {cmd if shell else shlex.join(cmd)}\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
    return p.stdout


BANNER = re.compile(r"^(Identity added|Initializing new SSH agent)")


def cluster(cmd, **kw):
    """Run a shell command on the cluster login host."""
    out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", cfg["SSH_HOST"], cmd], **kw)
    return "\n".join(l for l in out.splitlines() if not BANNER.match(l))


def poc(cmd, **kw):
    """Run a shell command on the POC cluster login host (via SSH_HOST)."""
    return cluster(f"ssh -o BatchMode=yes -o ConnectTimeout=20 {cfg['POC_HOST']} {shlex.quote(cmd)}", **kw)


# Where tests run: the ood-poc portal and the POC cluster (default, used by
# every release), or production (--target production: the production portal's
# files, jobs on SSH_HOST; --gpu: Jupyter on the GPU partition).
TARGET = {"name": "ood-poc", "ns": "ood-poc", "gpu": False}


def kube_poc(cmd):
    return run(["ssh", "-o", "BatchMode=yes", cfg["KUBE_HOST"], f"kubectl -n {TARGET['ns']} {cmd}"])


def notify(subject, body):
    log(f"notify: {subject}")
    text = f"{body}\n\n-- \nrelease_bot.py on {os.uname().nodename}, log: {LOG}\n"
    cluster(f"mail -s {shlex.quote('[OOD] ' + subject)} {shlex.quote(cfg['NOTIFY_EMAIL'])}", input=text)


def http_json(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def render_erb(app_dir, template_text, context):
    """Render a batch-connect template like OOD does (form values as context.<attr>)."""
    ruby = ("require 'erb'; require 'json'; require 'ostruct'; "
            "context = OpenStruct.new(JSON.parse(ARGV[0])); "
            "print ERB.new(STDIN.read, trim_mode: '-').result(binding)")
    return run(["docker", "run", "--rm", "-i", "-v", f"{app_dir}:/app:ro", "-w", "/app", "ruby:3-slim",
                "ruby", "-e", ruby, json.dumps(context)], input=template_text)


def upload(local, name):
    """Copy an image to IMAGES atomically; refuse to overwrite."""
    dest = f"{IMAGES}/{name}"
    if "EXISTS" in cluster(f"test -e {dest} && echo EXISTS || true"):
        raise RuntimeError(f"{dest} already exists; not overwriting it")
    total = run(["sha256sum", str(local)]).split()[0]
    run(["scp", "-q", str(local), f"{cfg['SSH_HOST']}:{IMAGES}/.{name}.part"])
    remote = cluster(f"sha256sum {IMAGES}/.{name}.part").split()[0]
    if remote != total:
        raise RuntimeError(f"checksum mismatch after upload: {total} vs {remote}")
    # The login node refuses to exec mv; python's rename works on the CIFS share.
    cluster(f"python3 -c 'import os; os.rename(\"{IMAGES}/.{name}.part\", \"{dest}\")'")
    log(f"uploaded {dest} (sha256 {total})")
    return total


NEED_GB = {"jupyter": 45, "rstudio_bioconductor_321": 12, "vscode_cli_tunnel": 6}


def check_disk(app):
    free = os.statvfs(STATE).f_bavail * os.statvfs(STATE).f_frsize / 1e9
    if free < NEED_GB.get(app, 10):
        raise RuntimeError(f"only {free:.0f} GB free in {STATE}; building {app} needs about "
                           f"{NEED_GB.get(app, 10)} GB. Manual step: free disk space, then re-run.")


def userns_wrapper():
    """Prefix that lets `apptainer build --fakeroot` work from the systemd timer.

    Ubuntu's AppArmor only lets a program make an unprivileged user namespace
    with capabilities if a profile allows it. apptainer ships one for its
    starter, but for a user without /etc/subuid ranges the build makes its
    root-mapped namespace from the apptainer command itself, which has none,
    and %post dies at once with "Could not write info to setgroups: Permission
    denied". It works from a terminal in desktop VS Code only because VS Code's
    profile allows it and child processes inherit that. So when this process
    has no profile, run the build under apptainer's.
    """
    try:
        restricted = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1"
        label = Path("/proc/self/attr/current").read_text().strip("\x00\n ")
    except OSError:
        return []
    if restricted and label == "unconfined" and Path("/etc/apparmor.d/apptainer").exists():
        return ["aa-exec", "-p", "apptainer", "--"]
    return []


def apptainer_build(sif, deffile, args, cwd):
    # Keep apptainer's scratch and cache with the bot's state, not in /tmp
    # (the host's /tmp is also what %post sees during a build).
    env = {**os.environ, "APPTAINER_TMPDIR": str(STATE / "build/tmp"), "APPTAINER_CACHEDIR": str(STATE / "build/cache")}
    (STATE / "build/tmp").mkdir(parents=True, exist_ok=True)
    cmd = userns_wrapper() + ["apptainer", "build", "--force", "--fakeroot"]
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    log(f"building {sif.name} ({', '.join(f'{k}={v}' for k, v in args.items())})")
    with (STATE / "logs" / f"{RUN_ID}-build-{sif.stem}.log").open("w") as f:
        try:
            p = subprocess.run(cmd + [str(sif), str(deffile)], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT,
                               timeout=2 * 3600)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"apptainer build of {sif.name} took more than 2 h (stuck download?), see {f.name}")
    subprocess.run(["rm", "-rf", str(STATE / "build/cache")])
    if p.returncode:
        raise RuntimeError(f"apptainer build failed, see {f.name}")
    return dict(l.split("=", 1) for l in run(["apptainer", "exec", str(sif), "cat", "/etc/ood-image-versions"]).split())


# --------------------------------------------------------------------------- repo + POC
REPO = STATE / "repo"


def checkout(ref):
    if not (REPO / ".git").exists():
        run(["git", "clone", "-q", f"git@github.com:{REPO_SLUG}.git", str(REPO)])
    run(["git", "fetch", "-q", "origin"], cwd=REPO)
    run(["git", "checkout", "-q", "-B", "bot-work", f"origin/{ref}"], cwd=REPO)
    run(["git", "clean", "-qfdx"], cwd=REPO)
    return run(["git", "rev-parse", "HEAD"], cwd=REPO).strip()


def remote_branch_exists(branch):
    return bool(run(["git", "ls-remote", "--heads", "origin", branch], cwd=REPO).strip())


def publish_to_poc(ref):
    """Publish an onDemand ref on ood-poc and wait for the deploy workflow."""
    repo, wf = cfg["POC_DEPLOY_REPO"], "deploy-ood-poc.yml"
    started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
    run(["gh", "workflow", "run", wf, "-R", repo, "--ref", cfg["POC_DEPLOY_REF"], "-f", f"apps_ref={ref}"])
    run_id = None
    for _ in range(30):
        time.sleep(10)
        runs = json.loads(run(["gh", "run", "list", "-R", repo, "--workflow", wf, "-L", "5",
                               "--json", "databaseId,createdAt,event"]))
        new = [r for r in runs if r["event"] == "workflow_dispatch"
               and dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= started]
        if new:
            run_id = new[0]["databaseId"]
            break
    if not run_id:
        raise RuntimeError("ood-poc deploy run did not start")
    log(f"ood-poc deploy run {run_id}")
    # The shared runner can be busy for a long time (Odoo deploys).
    run(["gh", "run", "watch", str(run_id), "-R", repo, "--exit-status", "-i", "30"], timeout=4 * 3600)


def poc_file(app, rel):
    """A file as the ood-poc portal serves it."""
    return kube_poc(f"exec deploy/ood-portal -c ood-container -- cat /var/www/ood/apps/sys/{app}/{rel}")


def poc_job(name, files, sbatch, timeout_min=30, extra=""):
    """Run a test job on the target cluster with the given files; return its output."""
    d = f"ood_release_bot_{name}_{RUN_ID}"
    local = STATE / "jobs" / d
    local.mkdir(parents=True)
    for rel, content in {**files, "test.sbatch": sbatch}.items():
        p = local / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            p.write_bytes(content.read_bytes())
            p.chmod(content.stat().st_mode)
        else:
            p.write_text(content)
    # Homes are shared between the login hosts; tar cannot run on SSH_HOST.
    cluster(f"mkdir -p {d}")
    run(f"scp -qr {shlex.quote(str(local))}/. {cfg['SSH_HOST']}:{d}/")
    if TARGET["name"] == "production":
        on, sb, part = cluster, "", TARGET["partition"]
    else:
        on, sb, part = poc, cfg["POC_SLURM_BIN"] + "/", cfg["POC_PARTITION"]
    job = on(f"cd {d} && {sb}sbatch --parsable -p {part} {extra} -J ood_release_bot_{name} -t {timeout_min} -o test.out test.sbatch").strip().splitlines()[-1]
    log(f"{TARGET['name']} job {job} ({name}, partition {part})")
    for _ in range(timeout_min * 4 + 60):
        if not on(f"{sb}squeue -h -j {job} 2>/dev/null || true").strip():
            break
        time.sleep(15)
    out = cluster(f"cat {d}/test.out 2>/dev/null || true")
    if os.environ.get("RELEASE_BOT_KEEP_JOBS"):
        log(f"keeping ~/{d} on the cluster (RELEASE_BOT_KEEP_JOBS)")
    else:
        cluster(f"rm -rf {d}", check=False)
    log(f"POC job output:\n{out}")
    return job, out


def slurm_check(app, image):
    """Files and job lines that run ood_slurm.sh's self-test in IMAGE: the
    cluster's Slurm commands inside the app's container (ood-poc's copy)."""
    try:
        helper = poc_file(app, "template/ood_slurm.sh")
    except RuntimeError:
        return {}, 'echo "ood-slurm: not in this ref"\n'
    return {"ood_slurm.sh": helper}, f"""module load singularity >/dev/null 2>&1
( source ./ood_slurm.sh; ood_slurm_setup "{image}" "$PWD/slurmcheck" >/dev/null; ood_slurm_selftest "{image}" ) 2>&1 | tail -1
"""


SLURM_OK = re.compile(r"^ood-slurm: (ok|disabled|not in this ref)", re.M)


SBATCH_HEAD = """#!/bin/bash
#SBATCH -c 2
#SBATCH --mem=8G
cd "$SLURM_SUBMIT_DIR"
echo "node=$(hostname)"
# OOD's job environment provides the module function to the app scripts.
source /etc/profile.d/lmod.sh; [[ $(type -t module) == "function" ]] && export -f module
export port=$(python3 -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1])')
"""


# --------------------------------------------------------------------------- plugins
class Release:
    """One new version. The form value is the description itself (label ==
    key), because OOD's session card shows the value; `short` (e.g. "3.24",
    "2026.11") names the image file, the branch and the test job."""
    def __init__(self, short, label, image, deffile, build_args, summary):
        self.short, self.label, self.image, self.deffile = short, label, image, deffile
        self.build_args, self.summary = build_args, summary

    @property
    def key(self):
        return self.label


def slug(text):
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")[:60]


def offered_keys(app):
    """Descriptive keys of the images table in an app's script.sh.erb."""
    return re.findall(r'^    "([^"]+)" =>', (REPO / app / "template/script.sh.erb").read_text(), re.M)


def resolve_key(app, text):
    """Map a short name ("3.23", "2026.11", "1.144.0", "ehmm") to a key."""
    script = (REPO / app / "template/script.sh.erb").read_text()
    keys = offered_keys(app)
    if text in keys:
        return text
    m = re.search(rf'"{re.escape(text)}" => "([^"]+)"', script)     # an old short name (aliases)
    if m and m.group(1) in keys:
        return m.group(1)
    rows = dict(re.findall(r'^    "([^"]+)" => (.*)$', script, re.M))   # key -> image path(s)
    hits = [k for k in keys if text.lower() in k.lower() or text in rows.get(k, "")]
    if len(hits) != 1:
        raise RuntimeError(f"{text!r} matches {len(hits)} versions of {app}: {keys}")
    return hits[0]


def insert_after(text, anchor_re, line, what):
    new, n = re.subn(anchor_re, lambda m: m.group(0) + line, text, count=1)
    if n != 1:
        raise RuntimeError(f"could not find {what}")
    return new


class RStudio:
    app = "rstudio_bioconductor_321"
    title = "RStudio"

    def offered(self):
        return [m.group(1) for k in offered_keys(self.app) if (m := re.match(r"Bioconductor (\d+\.\d+),", k))]

    def detect(self):
        best, url = None, "https://hub.docker.com/v2/repositories/bioconductor/tidyverse/tags?page_size=100"
        while url:
            d = http_json(url)
            for t in d["results"]:
                m = re.fullmatch(r"(\d+)\.(\d+)-R-(\d+)\.(\d+)\.(\d+)", t["name"])
                if m and (best is None or tuple(map(int, m.groups())) > best[0]):
                    best = (tuple(map(int, m.groups())), t["name"])
            url = d.get("next")
        bioc, tag = f"{best[0][0]}.{best[0][1]}", best[1]
        log(f"[{self.app}] offers {self.offered()}; latest bioconductor/tidyverse: {tag}")
        if bioc in self.offered():
            return []
        rstudio = latest_rstudio()
        r = tag.split("-R-")[1]
        return [Release(bioc, f"Bioconductor {bioc}, R {r}, RStudio {'.'.join(rstudio.split('.')[:2])}",
                        f"bioconductor_{bioc}.sif", "rstudio_bioconductor.def",
                        {"BIOC_TAG": tag, "RSTUDIO_VERSION": rstudio},
                        f"Bioconductor {bioc} (R {r}, RStudio Server {rstudio})")]

    def patch(self, rel):
        d = REPO / self.app
        f = d / "form.yml"
        f.write_text(insert_after(f.read_text(), r"  bioc_version:\n(?:    .*\n)*?    options:\n",
                                  f'      - ["{rel.label}", "{rel.label}"]\n', "bioc_version options in form.yml"))
        f = d / "template/script.sh.erb"
        f.write_text(insert_after(f.read_text(), r"  images = \{\n",
                                  f'    "{rel.key}" => ["{IMAGES}/{rel.image}", "rstudio_server_bioconductor_{rel.short}"],\n',
                                  "images table in script.sh.erb"))

    def poc_test(self, key):
        script = render_erb(REPO / self.app, poc_file(self.app, "template/script.sh.erb"), {"bioc_version": key})
        if f'echo "Version {key}:' not in script:
            raise RuntimeError(f"ood-poc's script.sh.erb does not offer {key}")
        workdir = re.search(r'^workdir="\$\{HOME\}/([^"]+)"$', script, re.M).group(1)
        image = re.search(r'export RSTUDIO_SERVER_IMAGE="([^"]+)"', script).group(1)
        sfiles, sline = slurm_check(self.app, image)
        sbatch = SBATCH_HEAD + sline + """bash ./script.sh > rserver.log 2>&1 &
pid=$!
python3 ./probe.py "$port" "$USER"
kill $pid; pkill -u "$USER" -f "rserver --www-port $port"; wait
"""
        job, out = poc_job(f"rstudio_{slug(key)}", {"script.sh": script, "probe.py": HERE / "probes/rstudio.py",
                                               "bin/auth": REPO / self.app / "template/bin/auth", **sfiles}, sbatch)
        ok = (SLURM_OK.search(out) and "sign_in: 200" in out and re.search(r"^ide: 200 .*title: RStudio", out, re.M)
              and re.search(rf"^rsession: .*--r-libs-user \S*/{re.escape(workdir)}/R/user-libs", out, re.M))
        return bool(ok), job, out


class Jupyter:
    app = "jupyter"
    title = "Jupyter Lab"

    # Cell Ranger releases come from 10x's signed links, so they are not
    # downloaded here: a maintainer puts cellranger-X.Y.Z.tar.gz (and
    # cellranger-arc-X.Y.Z.tar.gz) in IMAGES, and the next image uses the newest.
    CR_FILE = re.compile(r"^cellranger-(\d+\.\d+\.\d+)\.tar(?:\.gz)?$")
    ARC_FILE = re.compile(r"^cellranger-arc-(\d+\.\d+\.\d+)\.tar(?:\.gz)?$")
    # Image with Cell Ranger 9.0.1 / ARC 2.0.2 in /opt, used to re-create those
    # tarballs when the copies in IMAGES are damaged.
    CELLRANGER_FALLBACK_IMAGE = "jupyter-cellranger-gpu-cuda-13.sif"

    def offered(self):
        """(jupyterlab, torch, cellranger) major.minor of each offered image.

        Labels without "Cell Ranger X.Y" are the cuda11/cuda13 images (9.0).
        """
        form = (REPO / self.app / "form.yml").read_text()
        out = []
        for label in re.findall(r'^ +- \["([^"]*JupyterLab [^"]*)"', form, re.M):
            jl = re.search(r"JupyterLab (\d+)\.(\d+)", label)
            t = re.search(r"PyTorch (\d+)\.(\d+)", label)
            cr = re.search(r"Cell Ranger (\d+)\.(\d+)", label)
            if jl and t:
                out.append((tuple(map(int, jl.groups())), tuple(map(int, t.groups())),
                            tuple(map(int, cr.groups())) if cr else (9, 0)))
        return out

    def cellranger_files(self):
        """{"cellranger": (version, file), "arc": (version, file)}: newest in IMAGES."""
        names = cluster(f"ls -1 {IMAGES}").split()
        newest = {}
        for kind, rx in (("cellranger", self.CR_FILE), ("arc", self.ARC_FILE)):
            found = [(tuple(map(int, m.group(1).split("."))), m.group(1), n) for n in names if (m := rx.match(n))]
            if not found:
                raise RuntimeError(f"no {kind} tarball in {IMAGES}")
            _, v, n = max(found)
            newest[kind] = (v, n)
        return newest

    def detect(self):
        jl = http_json("https://pypi.org/pypi/jupyterlab/json")["info"]["version"]
        torch, tv = self.torch_pair()
        cr = self.cellranger_files()
        cr_v, arc_v = cr["cellranger"][0], cr["arc"][0]
        mm = lambda v: tuple(map(int, v.split(".")[:2]))
        want = (mm(jl), mm(torch), mm(cr_v))
        have = self.offered()
        log(f"[{self.app}] offers (jupyterlab, torch, cellranger) "
            f"{[tuple('.'.join(map(str, x)) for x in h) for h in have]}; latest jupyterlab {jl}, torch {torch} "
            f"(torchvision {tv}), Cell Ranger {cr_v}, ARC {arc_v} (from {IMAGES})")
        if any(all(h[i] >= want[i] for i in range(3)) for h in have):
            return []
        short = dt.date.today().strftime("%Y.%m")     # names the image file
        script = (REPO / self.app / "template/script.sh.erb").read_text()
        while f"jupyter-cellranger_{short}.sif" in script:
            short += ".1"
        # The CUDA version PyTorch was built for is filled in after the build.
        label = (f"Python 3.12, JupyterLab {'.'.join(map(str, want[0]))}, "
                 f"PyTorch {'.'.join(map(str, want[1]))} (CUDA ?), Cell Ranger {'.'.join(map(str, want[2]))}")
        return [Release(short, label, f"jupyter-cellranger_{short}.sif", "jupyter-cellranger.def",
                        {"JUPYTERLAB_VERSION": jl, "TORCH_VERSION": torch, "TORCHVISION_VERSION": tv,
                         "CELLRANGER_VERSION": cr_v, "CELLRANGER_ARC_VERSION": arc_v},
                        f"image {short}: JupyterLab {jl}, PyTorch {torch}, Cell Ranger {cr_v} (ARC {arc_v}), Python 3.12")]

    @staticmethod
    def label_after_build(rel, versions):
        rel.label = rel.label.replace("(CUDA ?)", f"(CUDA {versions.get('torch_cuda', '?')})")
        # The scverse stack is not pinned: name the versions the build got.
        if "scanpy" in versions and "liana" in versions:
            mm = lambda v: ".".join(v.split(".")[:2])
            rel.label += f", scanpy {mm(versions['scanpy'])}, LIANA+ {mm(versions['liana'])}"
            rel.summary += f", scanpy {versions['scanpy']}, LIANA+ {versions['liana']}"

    @staticmethod
    def torch_pair():
        """Newest PyTorch that has a matching torchvision release.

        torchvision follows torch by days or weeks, so a brand-new torch is
        used only once its torchvision exists (read from torchvision's own
        torch requirement, e.g. "torch==2.14.0" or "torch>=2.14.0").
        """
        num = lambda v: [int(x) for x in v.split(".")]
        stable = lambda rels: sorted((v for v in rels if re.fullmatch(r"\d+(\.\d+)+", v)), key=num)
        tv = stable(http_json("https://pypi.org/pypi/torchvision/json")["releases"])[-1]
        req = http_json(f"https://pypi.org/pypi/torchvision/{tv}/json")["info"]["requires_dist"] or []
        m = next((re.search(r"torch\s*(==|>=)\s*(\d+)\.(\d+)", r) for r in req if r.startswith("torch")), None)
        if not m:
            raise RuntimeError(f"torchvision {tv} has no torch requirement: {req}")
        torch_releases = stable(http_json("https://pypi.org/pypi/torch/json")["releases"])
        same_minor = [v for v in torch_releases if num(v)[:2] == [int(m.group(2)), int(m.group(3))]]
        if not same_minor:
            raise RuntimeError(f"no torch {m.group(2)}.{m.group(3)}.x release for torchvision {tv}")
        return same_minor[-1], tv

    def build_context(self, rel):
        """Put cellranger.tar.gz and cellranger-arc.tar.gz in the build directory.

        Uses the tarballs in IMAGES when intact; a damaged one is re-created
        from the same release installed in CELLRANGER_FALLBACK_IMAGE (if it
        has it) and the maintainer is emailed to replace it.
        """
        d = STATE / "build" / "jupyter-ctx"
        d.mkdir(parents=True, exist_ok=True)
        intact = lambda p: subprocess.run(["gzip", "-t", str(p)], capture_output=True).returncode == 0
        files = self.cellranger_files()
        damaged = []
        for kind, fixed, opt_prefix in (("cellranger", "cellranger.tar.gz", "cellranger"),
                                        ("arc", "cellranger-arc.tar.gz", "cellranger-arc")):
            version, name = files[kind]
            cache = d / f"{opt_prefix}-{version}.tar.gz"       # per-version cache
            if not (cache.exists() and intact(cache)):
                run(["scp", "-q", f"{cfg['SSH_HOST']}:{IMAGES}/{name}", str(cache)])
            if not intact(cache):
                damaged.append(name)
                src = STATE / "build" / self.CELLRANGER_FALLBACK_IMAGE
                if not src.exists():
                    log(f"{IMAGES}/{name} is damaged; copying {src.name} to re-create it")
                    run(["scp", "-q", f"{cfg['SSH_HOST']}:{IMAGES}/{src.name}", str(src)])
                opt_dir = f"{opt_prefix}-{version}"
                log(f"re-creating {name} from /opt/{opt_dir} in {src.name}")
                with cache.open("wb") as out:
                    p = subprocess.run(["apptainer", "exec", str(src), "tar", "-C", "/opt", "-czf", "-", opt_dir],
                                       stdout=out)
                if p.returncode or not intact(cache):
                    cache.unlink(missing_ok=True)
                    raise RuntimeError(f"{IMAGES}/{name} is damaged and {src.name} has no /opt/{opt_dir}. "
                                       f"Manual step: download {name} again from 10x Genomics.")
            target = d / fixed
            target.unlink(missing_ok=True)
            os.link(cache, target)
        if damaged:
            notify("release bot: Cell Ranger archives in the images folder are damaged",
                   f"These files in {IMAGES} fail 'gzip -t' (truncated or corrupt):\n  " + "\n  ".join(damaged) +
                   f"\n\nThe build used copies re-created from the same release installed in "
                   f"{self.CELLRANGER_FALLBACK_IMAGE}, kept in {d}.\n\n"
                   f"Manual step: replace the files in {IMAGES} with fresh downloads from 10x Genomics "
                   f"(or with the re-created copies in {d}).")
        return d

    def patch(self, rel):
        d = REPO / self.app
        f = d / "form.yml"
        f.write_text(insert_after(f.read_text(), r"  jupyter_image:\n(?:    .*\n)*?    options:\n",
                                  f'      - ["{rel.label}", "{rel.label}"]\n', "jupyter_image options in form.yml"))
        f = d / "template/script.sh.erb"
        f.write_text(insert_after(f.read_text(), r"  images = \{\n", f'    "{rel.key}" => "{IMAGES}/{rel.image}",\n',
                                  "images table in script.sh.erb"))

    def poc_test(self, key):
        gpu = TARGET["gpu"]
        script = render_erb(REPO / self.app, poc_file(self.app, "template/script.sh.erb"),
                            {"jupyter_image": key, "use_gpu": "Yes (GPU)" if gpu else "No (CPU node)"})
        m = re.search(r'export JUPYTERLAB_IMAGE="([^"]+)"', script)
        if not m or f'echo "Image {key}:' not in script:
            raise RuntimeError(f"ood-poc's script.sh.erb does not offer {key}")
        sfiles, sline = slurm_check(self.app, m.group(1))
        sbatch = SBATCH_HEAD + sline + r"""host=$(hostname)
password=pw$RANDOM$RANDOM; SALT=s$RANDOM
export CONFIG_FILE="$PWD/config.py"
cat > "$CONFIG_FILE" <<EOL
c.ServerApp.ip = '*'
c.ServerApp.port = ${port}
c.ServerApp.port_retries = 0
c.ServerApp.token = u''
c.ServerApp.password = u'sha1:${SALT}:$(echo -n "${password}${SALT}" | sha1sum | awk '{print $1}')'
c.ServerApp.base_url = '/node/${host}/${port}/'
c.ServerApp.open_browser = False
c.ServerApp.disable_check_xsrf = True
EOL
module load singularity >/dev/null 2>&1
# Same environment as the app: --cleanenv and the conda env activated.
singularity exec --cleanenv "%s" bash --norc -c 'source /opt/conda/etc/profile.d/conda.sh && conda activate jlab-env && python -c "import zmq, jupyterlab, numpy, torch; print(\"imports: torch\", torch.__version__, \"cuda\", torch.version.cuda, \"jupyterlab\", jupyterlab.__version__)"'
if [ -n "$TEST_GPU" ]; then
singularity exec --nv --cleanenv "%s" bash --norc -c 'source /opt/conda/etc/profile.d/conda.sh && conda activate jlab-env && python -c "import zmq, torch, torchvision; ok = torch.cuda.is_available(); print(\"gpu:\", ok, torch.cuda.get_device_name(0) if ok else \"\"); x = torch.rand(2000, 2000, device=\"cuda\"); print(\"gpu_matmul:\", bool((x @ x).sum() > 0)); b = torch.tensor([[0., 0, 10, 10], [1, 1, 11, 11]], device=\"cuda\"); print(\"gpu_nms:\", torchvision.ops.nms(b, torch.tensor([0.9, 0.8], device=\"cuda\"), 0.5).tolist())"' 2>&1 | tail -4
fi
# scverse stack (images that have it): scanpy, decoupler and LIANA+ on synthetic data.
singularity exec --cleanenv "%s" bash --norc -c 'source /opt/conda/etc/profile.d/conda.sh && conda activate jlab-env && python ./scverse.py' 2>&1 | tail -40
# Stale packages in the user's pip folder must not override the image's (images with sitecustomize.py).
singularity exec --cleanenv "%s" bash --norc -c 'source /opt/conda/etc/profile.d/conda.sh && conda activate jlab-env && python ./usersite.py' 2>&1 | tail -5
singularity exec --cleanenv "%s" bash --norc -c 'export PATH=/opt/cellranger:/opt/cellranger-arc:/opt/cellranger-9.0.1:/opt/cellranger-arc-2.0.2:$PATH; cellranger --version; cellranger-arc --version' 2>&1 | sed 's/^/cellranger: /'
bash ./script.sh > jupyter.log 2>&1 &
pid=$!
python3 ./probe.py "$port" "$host" "$password"
pkill -u "$USER" -f "port=${port}"; kill $pid 2>/dev/null; wait
""" % ((m.group(1),) * 5)
        if gpu:
            sbatch = sbatch.replace("cd \"$SLURM_SUBMIT_DIR\"\n", "cd \"$SLURM_SUBMIT_DIR\"\nexport TEST_GPU=1\n", 1)
        job, out = poc_job(f"jupyter_{slug(key)}", {"script.sh": script, "probe.py": HERE / "probes/jupyter.py",
                                                      "scverse.py": HERE / "probes/scverse.py",
                                                      "usersite.py": HERE / "probes/usersite.py", **sfiles}, sbatch,
                           extra="--gres=gpu:1" if gpu else "")
        ok = (SLURM_OK.search(out) and re.search(r"^imports: torch", out, re.M) and "login: 200" in out and "api_status: 200" in out
              and "lab: 200 True" in out and "kernel: idle" in out
              and re.search(r"^cellranger: cellranger (cellranger-)?\d+\.\d+\.\d+", out, re.M)
              and re.search(r"^cellranger: cellranger-arc (cellranger-arc-)?\d+\.\d+\.\d+", out, re.M))
        # Images labelled with LIANA+ must pass the scverse check; others skip it.
        ok = ok and (re.search(r"^scverse: ok", out, re.M) if "LIANA+" in key else "scverse: FAILED" not in out)
        ok = ok and re.search(r"^usersite: (ok|not protected)", out, re.M)
        if gpu:
            ok = ok and re.search(r"^gpu: True", out, re.M) and "gpu_matmul: True" in out and "gpu_nms: [0]" in out
        return bool(ok), job, out


class VSCode:
    app = "vscode_cli_tunnel"
    title = "VS Code Tunnel"

    def offered(self):
        return [m.group(1) for k in offered_keys(self.app) if (m := re.match(r"VS Code (\d+\.\d+\.\d+),", k))]

    # VS Code ships a minor release about every week, plus recovery patches.
    # The image only provides the tunnel CLI (the editor installs its own
    # matching server on connect), so update about once a quarter, to a minor
    # release that has settled.
    SETTLED_DAYS = 14   # newest patch of the chosen minor is at least this old
    MAX_AGE_DAYS = 90   # update only once the offered version is this old

    @staticmethod
    def release_dates():
        """{version: date} for VS Code stable releases (GitHub releases)."""
        dates, page = {}, 1
        while page <= 5:
            rels = http_json(f"https://api.github.com/repos/microsoft/vscode/releases?per_page=100&page={page}")
            if not rels:
                break
            for r in rels:
                if re.fullmatch(r"\d+\.\d+\.\d+", r["tag_name"]) and not r.get("prerelease"):
                    dates[r["tag_name"]] = dt.date.fromisoformat(r["published_at"][:10])
            page += 1
        return dates

    def detect(self):
        dates = self.release_dates()
        today = dt.date.today()
        num = lambda v: tuple(int(x) for x in v.split("."))
        minors = {}
        for v in dates:
            minors.setdefault(num(v)[:2], []).append(v)
        settled = [max(vs, key=num) for m, vs in minors.items()
                   if (today - max(dates[v] for v in vs)).days >= self.SETTLED_DAYS]
        target = max(settled, key=num)
        cur = max(self.offered(), key=num, default=None)
        cur_age = (today - dates[cur]).days if cur in dates else None
        log(f"[{self.app}] offers {self.offered()}, newest {cur}"
            f"{f' ({cur_age} days old)' if cur_age is not None else ''}; newest VS Code {max(dates, key=num)}, "
            f"newest settled (>= {self.SETTLED_DAYS} days) {target}")
        if cur and (num(cur) >= num(target) or (cur_age is not None and cur_age < self.MAX_AGE_DAYS)):
            return []
        return [Release(target, f"VS Code {target}, Slurm client 20.11", f"vscode-tunnel_{target}.sif",
                        "vscode-tunnel.def", {"VSCODE_VERSION": target}, f"VS Code {target}")]

    def patch(self, rel):
        d = REPO / self.app
        f = d / "form.yml.erb"
        f.write_text(insert_after(f.read_text(), r"  vscode_version:\n(?:    .*\n)*?    options:\n",
                                  f'      - ["{rel.label}", "{rel.label}"]\n', "vscode_version options in form.yml.erb"))
        f = d / "template/script.sh.erb"
        f.write_text(insert_after(f.read_text(), r"  images = \{\n", f'    "{rel.key}" => "{IMAGES}/{rel.image}",\n',
                                  "images table in script.sh.erb"))

    def poc_test(self, key):
        # The tunnel itself needs an interactive Microsoft login; test the image
        # the POC-served script points at: CLI version, tunnel CLI, Slurm client.
        script = render_erb(REPO / self.app, poc_file(self.app, "template/script.sh.erb"), {"vscode_version": key})
        if f'echo "Version {key}:' not in script:
            raise RuntimeError(f"ood-poc's script.sh.erb does not offer {key}")
        image = re.search(r'^SINGULARITY_IMAGE="([^"]+)"$', script, re.M).group(1)
        sfiles, sline = slurm_check(self.app, image)
        sbatch = SBATCH_HEAD + sline + f"""module load singularity >/dev/null 2>&1
echo "image={image}"
singularity exec {image} /usr/container/bin/code --version | head -1 | sed 's/^/code: /'
singularity exec {image} /usr/container/bin/code tunnel user show 2>&1 | head -1 | sed 's/^/tunnel: /'
# sbatch needs the node's slurm.conf, which the app's script prepares; here
# check the client is installed and the --export=NONE defaults are set.
singularity exec --cleanenv {image} sh -c 'test -x /usr/bin/sbatch && echo sbatch=ok; echo "export=$SBATCH_EXPORT"; test -f /etc/profile.d/slurm-export.sh && echo profile=ok' 2>&1 | sed 's/^/slurm: /'
"""
        job, out = poc_job(f"vscode_{slug(key)}", sfiles, sbatch, timeout_min=10)
        version = re.match(r"VS Code (\S+),", key).group(1)
        ok = (SLURM_OK.search(out) and re.search(rf"^code: code {re.escape(version)}\b", out, re.M) and re.search(r"^tunnel: \S", out, re.M)
              and "slurm: sbatch=ok" in out and "slurm: export=NONE" in out and "slurm: profile=ok" in out)
        return bool(ok), job, out


def latest_rstudio():
    out = run(["curl", "-sSI", "-m", "30", "https://rstudio.org/download/latest/stable/server/jammy/rstudio-server-latest-amd64.deb"])
    loc = [l.split(None, 1)[1].strip() for l in out.splitlines() if l.lower().startswith("location:")]
    m = re.search(r"rstudio-server-(\d+\.\d+\.\d+)-(\d+)-amd64\.deb$", loc[-1] if loc else "")
    if not m:
        raise RuntimeError(f"could not determine the latest RStudio Server (redirect: {loc})")
    return f"{m.group(1)}+{m.group(2)}"


PLUGINS = {p.app: p for p in (RStudio(), Jupyter(), VSCode())}


# --------------------------------------------------------------------------- flow
def summary_of(out):
    keep = r"^(node=|gpu:|gpu_matmul:|gpu_nms:|listening|sign_in|do_sign_in|ide:|rsession:|imports:|cellranger:|login|api_status|lab:|kernel|image=|code:|tunnel:|slurm:|scverse:|usersite:|ood-slurm:)"
    return "\n".join(l[:160] for l in out.splitlines() if re.match(keep, l))


def release(plugin, rel, onto=None):
    """Build, upload, add and POC-test one release.

    Normally on a new branch off master, ending in a PR. With onto=BRANCH the
    version is committed onto that existing branch instead (e.g. a PR still
    in review) and no PR or email is made; failures still raise.
    """
    branch = onto or f"release-bot/{plugin.app}-{rel.short}"
    if not onto and remote_branch_exists(branch):
        log(f"[{plugin.app}] {branch} already exists (PR pending); skipping")
        return
    check_disk(plugin.app)
    ctx = plugin.build_context(rel) if hasattr(plugin, "build_context") else REPO / plugin.app
    sif = STATE / "build" / rel.image
    versions = apptainer_build(sif, REPO / plugin.app / rel.deffile, rel.build_args, ctx)
    log(f"image versions: {versions}")
    if hasattr(plugin, "label_after_build"):
        plugin.label_after_build(rel, versions)
    sha = upload(sif, rel.image)
    sif.unlink()

    if not onto:
        run(["git", "checkout", "-q", "-b", branch], cwd=REPO)
    plugin.patch(rel)
    run(["git", "-c", "user.name=ood release bot", "commit", "-qam",
         f"{plugin.title}: add {rel.summary}\n\nImage {IMAGES}/{rel.image} (sha256 {sha}), built by "
         f".github/release-bot from {plugin.app}/{rel.deffile} and tested on ood-poc."], cwd=REPO)
    run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=REPO)

    publish_to_poc(branch)
    ok, job, out = plugin.poc_test(rel.key)
    image_versions = "\n".join(f"{k}={v}" for k, v in versions.items())
    if not ok:
        raise RuntimeError(f"ood-poc test of {rel.summary} failed (POC job {job}).\nBranch {branch} and image "
                           f"{IMAGES}/{rel.image} were left in place.\n\n{out}")
    if onto:
        log(f"[{plugin.app}] added {rel.summary} onto {onto}; ood-poc test passed (job {job})\n"
            f"{image_versions}\n{summary_of(out)}")
        return
    body = (f"Automated by `.github/release-bot/release_bot.py`.\n\n"
            f"- Image: `{IMAGES}/{rel.image}` (sha256 `{sha}`), built from `{plugin.app}/{rel.deffile}` "
            f"with {', '.join(f'`{k}={v}`' for k, v in rel.build_args.items())}\n"
            f"- Adds **{rel.label}** to *{plugin.title}*; the form default is unchanged.\n\n"
            f"Image versions:\n```\n{image_versions}\n```\n"
            f"Tested on **ood-poc**: branch published to the POC portal, job script rendered from the files "
            f"the POC serves and run on `{cfg['POC_PARTITION']}` (job {job}):\n```\n{summary_of(out)}\n```\n"
            f"ood-poc still serves this branch, so you can click through it there before merging. "
            f"Merging publishes to production in place (no restart).")
    pr = run(["gh", "pr", "create", "-R", REPO_SLUG, "--base", "master", "--head", branch,
              "--title", f"{plugin.title}: add {rel.summary}", "--body", body], cwd=REPO).strip()
    notify(f"{plugin.title}: {rel.summary} is ready - review and merge",
           f"{rel.summary} was built, uploaded and passed the test on ood-poc.\n\n"
           f"Manual step: review and merge the pull request:\n  {pr}\n\n"
           f"ood-poc serves the branch now if you want to try it first. Merging publishes it on the "
           f"production portal (in place, no restart). The default version stays as it is.\n\n{summary_of(out)}")
    log(f"[{plugin.app}] done: {pr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=sorted(PLUGINS))
    ap.add_argument("--test-existing", metavar="APP:VERSION")
    ap.add_argument("--ref", default="master", help="onDemand ref to test or dry-run against")
    ap.add_argument("--onto", metavar="BRANCH", help="add new versions onto this existing branch (no PR, no email)")
    ap.add_argument("--target", choices=["ood-poc", "production"], default="ood-poc",
                    help="with --test-existing: test what this portal serves, on its cluster")
    ap.add_argument("--gpu", action="store_true", help="with --test-existing jupyter:... on production: use a GPU node")
    a = ap.parse_args()
    if a.onto:
        a.ref = a.onto
    elif a.ref != "master" and not (a.dry_run or a.test_existing):
        sys.exit("--ref is only for --dry-run and --test-existing; releases start from master (or use --onto)")

    (STATE / "logs").mkdir(parents=True, exist_ok=True)
    (STATE / "build").mkdir(parents=True, exist_ok=True)
    for line in CONF.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    if "NOTIFY_EMAIL" not in cfg:
        sys.exit(f"set NOTIFY_EMAIL in {CONF}")
    lock = open(STATE / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another release_bot.py run is in progress")

    if a.test_existing:
        app, key = a.test_existing.split(":", 1)
        plugin = PLUGINS[app]
        try:
            checkout(a.ref)
            key = resolve_key(app, key)
            if a.target == "production":
                TARGET.update(name="production", ns="production", gpu=a.gpu,
                              partition=cfg.get("PROD_GPU_PARTITION", "test-gpu") if a.gpu else cfg.get("PROD_PARTITION", "test2"))
            else:
                publish_to_poc(a.ref)
            ok, job, out = plugin.poc_test(key)
        except Exception as e:
            notify(f"release bot: ood-poc test of {app}:{key} FAILED", f"{e}\n\n{traceback.format_exc()}")
            raise
        log(f"{TARGET['name']}{' GPU' if TARGET['gpu'] else ''} test of {app}:{key}: {'passed' if ok else 'FAILED'}\n{summary_of(out)}")
        if not ok:  # only failures need the maintainer
            notify(f"release bot: ood-poc test of {app}:{key} FAILED",
                   f"Test run only (--test-existing {app}:{key}, ref {a.ref}); nothing was built or changed.\n"
                   f"POC job {job}.\n\n{out}")
        return 0 if ok else 1

    failed = False
    for name, plugin in PLUGINS.items():
        if a.only and name != a.only:
            continue
        try:
            checkout(a.ref)
            for rel in plugin.detect():
                log(f"[{name}] new: {rel.summary} -> {rel.image}")
                if not a.dry_run:
                    release(plugin, rel, onto=a.onto)
                    checkout(a.ref)
        except Exception as e:
            failed = True
            log(f"[{name}] FAILED: {e}")
            notify(f"release bot: {plugin.title} FAILED",
                   f"{e}\n\nManual step: read the log, fix the cause, then re-run\n"
                   f"  systemctl --user start ood-release-bot.service\n\n{traceback.format_exc()[-3000:]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
