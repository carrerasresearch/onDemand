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


def kube_poc(cmd):
    return run(["ssh", "-o", "BatchMode=yes", cfg["KUBE_HOST"], f"kubectl -n ood-poc {cmd}"])


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


def apptainer_build(sif, deffile, args, cwd):
    # Keep apptainer's scratch and cache with the bot's state, not in /tmp
    # (the host's /tmp is also what %post sees during a build).
    env = {**os.environ, "APPTAINER_TMPDIR": str(STATE / "build/tmp"), "APPTAINER_CACHEDIR": str(STATE / "build/cache")}
    (STATE / "build/tmp").mkdir(parents=True, exist_ok=True)
    cmd = ["apptainer", "build", "--force", "--fakeroot"]
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    log(f"building {sif.name} ({', '.join(f'{k}={v}' for k, v in args.items())})")
    with (STATE / "logs" / f"{RUN_ID}-build-{sif.stem}.log").open("w") as f:
        p = subprocess.run(cmd + [str(sif), str(deffile)], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
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


def poc_job(name, files, sbatch, timeout_min=30):
    """Run a test job on the POC cluster with the given files; return its output."""
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
    sb = cfg["POC_SLURM_BIN"]
    job = poc(f"cd {d} && {sb}/sbatch --parsable -p {cfg['POC_PARTITION']} -J ood_release_bot_{name} -t {timeout_min} -o test.out test.sbatch").strip().splitlines()[-1]
    log(f"POC job {job} ({name})")
    for _ in range(timeout_min * 4 + 60):
        if not poc(f"{sb}/squeue -h -j {job} 2>/dev/null || true").strip():
            break
        time.sleep(15)
    out = cluster(f"cat {d}/test.out 2>/dev/null || true")
    cluster(f"rm -rf {d}", check=False)
    log(f"POC job output:\n{out}")
    return job, out


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
    def __init__(self, key, label, image, deffile, build_args, summary):
        self.key, self.label, self.image, self.deffile = key, label, image, deffile
        self.build_args, self.summary = build_args, summary


def insert_after(text, anchor_re, line, what):
    new, n = re.subn(anchor_re, lambda m: m.group(0) + line, text, count=1)
    if n != 1:
        raise RuntimeError(f"could not find {what}")
    return new


class RStudio:
    app = "rstudio_bioconductor_321"
    title = "Bioconductor"

    def offered(self):
        return re.findall(r'^ +"(\d+\.\d+)" =>', (REPO / self.app / "template/script.sh.erb").read_text(), re.M)

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
        return [Release(bioc, f"{bioc} (R {'.'.join(r.split('.')[:2])}, RStudio {'.'.join(rstudio.split('.')[:2])})",
                        f"bioconductor_{bioc}.sif", "rstudio_bioconductor.def",
                        {"BIOC_TAG": tag, "RSTUDIO_VERSION": rstudio},
                        f"Bioconductor {bioc} (R {r}, RStudio Server {rstudio})")]

    def patch(self, rel):
        d = REPO / self.app
        f = d / "form.yml"
        f.write_text(insert_after(f.read_text(), r"  bioc_version:\n(?:    .*\n)*?    options:\n",
                                  f'      - ["{rel.label}", "{rel.key}"]\n', "bioc_version options in form.yml"))
        f = d / "template/script.sh.erb"
        f.write_text(insert_after(f.read_text(), r"  images = \{\n",
                                  f'    "{rel.key}" => ["{IMAGES}/{rel.image}", "rstudio_server_bioconductor_{rel.key}"],\n',
                                  "images table in script.sh.erb"))

    def poc_test(self, key):
        script = render_erb(REPO / self.app, poc_file(self.app, "template/script.sh.erb"), {"bioc_version": key})
        if f'echo "Version {key}:' not in script:
            raise RuntimeError(f"ood-poc's script.sh.erb does not offer {key}")
        workdir = re.search(r'^workdir="\$\{HOME\}/([^"]+)"$', script, re.M).group(1)
        sbatch = SBATCH_HEAD + """bash ./script.sh > rserver.log 2>&1 &
pid=$!
python3 ./probe.py "$port" "$USER"
kill $pid; pkill -u "$USER" -f "rserver --www-port $port"; wait
"""
        job, out = poc_job(f"rstudio_{key}", {"script.sh": script, "probe.py": HERE / "probes/rstudio.py",
                                               "bin/auth": REPO / self.app / "template/bin/auth"}, sbatch)
        ok = ("sign_in: 200" in out and re.search(r"^ide: 200 .*title: RStudio", out, re.M)
              and re.search(rf"^rsession: .*--r-libs-user \S*/{re.escape(workdir)}/R/user-libs", out, re.M))
        return bool(ok), job, out


class Jupyter:
    app = "jupyter"
    title = "Jupyter Lab"

    def offered(self):
        form = (REPO / self.app / "form.yml").read_text()
        return [(int(a), int(b), int(c), int(d)) for a, b, c, d in
                re.findall(r"JupyterLab (\d+)\.(\d+), PyTorch (\d+)\.(\d+)", form)]

    def detect(self):
        jl = http_json("https://pypi.org/pypi/jupyterlab/json")["info"]["version"]
        torch, tv = self.torch_pair()
        jl_mm, t_mm = tuple(map(int, jl.split(".")[:2])), tuple(map(int, torch.split(".")[:2]))
        have = self.offered()
        log(f"[{self.app}] offers (jupyterlab, torch) {[(f'{a}.{b}', f'{c}.{d}') for a, b, c, d in have]}; "
            f"latest jupyterlab {jl}, torch {torch} (torchvision {tv})")
        if any((a, b) >= jl_mm and (c, d) >= t_mm for a, b, c, d in have):
            return []
        key = dt.date.today().strftime("%Y.%m")
        script = (REPO / self.app / "template/script.sh.erb").read_text()
        while f'"{key}" =>' in script:
            key += ".1"
        return [Release(key, f"{key} (Python 3.12, JupyterLab {jl_mm[0]}.{jl_mm[1]}, PyTorch {t_mm[0]}.{t_mm[1]})",
                        f"jupyter-cellranger_{key}.sif", "jupyter-cellranger.def",
                        {"JUPYTERLAB_VERSION": jl, "TORCH_VERSION": torch, "TORCHVISION_VERSION": tv},
                        f"image {key}: JupyterLab {jl}, PyTorch {torch}, torchvision {tv}, Python 3.12, Cell Ranger 9.0.1")]

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

    def build_context(self):
        """The .def copies the Cell Ranger tarballs from the build directory."""
        d = STATE / "build" / "jupyter-ctx"
        d.mkdir(parents=True, exist_ok=True)
        for tar in ("cellranger-9.0.1.tar", "cellranger-arc-2.0.2.tar"):
            if not (d / tar).exists():
                run(["scp", "-q", f"{cfg['SSH_HOST']}:{IMAGES}/{tar}", str(d / tar)])
        return d

    def patch(self, rel):
        d = REPO / self.app
        f = d / "form.yml"
        f.write_text(insert_after(f.read_text(), r"  jupyter_image:\n(?:    .*\n)*?    options:\n",
                                  f'      - ["{rel.label}", "{rel.key}"]\n', "jupyter_image options in form.yml"))
        f = d / "template/script.sh.erb"
        f.write_text(insert_after(f.read_text(), r"  images = \{\n", f'    "{rel.key}" => "{IMAGES}/{rel.image}",\n',
                                  "images table in script.sh.erb"))

    def poc_test(self, key):
        script = render_erb(REPO / self.app, poc_file(self.app, "template/script.sh.erb"),
                            {"jupyter_image": key, "use_gpu": cfg["POC_PARTITION"]})
        m = re.search(r'export JUPYTERLAB_IMAGE="([^"]+)"', script)
        if not m or f'echo "Image {key}:' not in script:
            raise RuntimeError(f"ood-poc's script.sh.erb does not offer {key}")
        sbatch = SBATCH_HEAD + r"""host=$(hostname)
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
singularity exec "%s" python -c 'import torch, jupyterlab, numpy; print("imports: torch", torch.__version__, "cuda", torch.version.cuda, "jupyterlab", jupyterlab.__version__)'
bash ./script.sh > jupyter.log 2>&1 &
pid=$!
python3 ./probe.py "$port" "$host" "$password"
pkill -u "$USER" -f "port=${port}"; kill $pid 2>/dev/null; wait
""" % m.group(1)
        job, out = poc_job(f"jupyter_{key}", {"script.sh": script, "probe.py": HERE / "probes/jupyter.py"}, sbatch)
        ok = (re.search(r"^imports: torch", out, re.M) and "login: 200" in out and "api_status: 200" in out
              and "lab: 200 True" in out and "kernel: idle" in out)
        return bool(ok), job, out


class VSCode:
    app = "vscode_cli_tunnel"
    title = "VS Code Tunnel"
    line = re.compile(r'^SINGULARITY_IMAGE="[^"]*"$', re.M)

    def offered(self):
        m = re.search(r'^SINGULARITY_IMAGE="[^"]*/vscode-tunnel_([\d.]+)\.sif"$',
                      (REPO / self.app / "template/script.sh.erb").read_text(), re.M)
        return [m.group(1)] if m else []   # [] = legacy unversioned image

    def detect(self):
        latest = http_json("https://update.code.visualstudio.com/api/releases/stable")[0]
        log(f"[{self.app}] offers {self.offered() or ['unversioned vscode-tunnel.sif']}; latest VS Code {latest}")
        if latest in self.offered():
            return []
        return [Release(latest, latest, f"vscode-tunnel_{latest}.sif", "vscode-tunnel.def",
                        {"VSCODE_VERSION": latest}, f"VS Code CLI {latest}")]

    def patch(self, rel):
        f = REPO / self.app / "template/script.sh.erb"
        new, n = self.line.subn(f'SINGULARITY_IMAGE="{IMAGES}/{rel.image}"', f.read_text(), count=1)
        if n != 1:
            raise RuntimeError("SINGULARITY_IMAGE line not found in script.sh.erb")
        f.write_text(new)

    def poc_test(self, key):
        # The tunnel itself needs an interactive Microsoft login; test the image
        # the POC-served script points at: CLI version, tunnel CLI, Slurm client.
        script = render_erb(REPO / self.app, poc_file(self.app, "template/script.sh.erb"), {})
        image = re.search(r'^SINGULARITY_IMAGE="([^"]+)"$', script, re.M).group(1)
        sbatch = SBATCH_HEAD + f"""module load singularity >/dev/null 2>&1
echo "image={image}"
singularity exec {image} /usr/container/bin/code --version | head -1 | sed 's/^/code: /'
singularity exec {image} /usr/container/bin/code tunnel user show 2>&1 | head -1 | sed 's/^/tunnel: /'
singularity exec {image} bash -lc 'sbatch --version; echo "export=$SBATCH_EXPORT"' 2>&1 | sed 's/^/slurm: /'
"""
        job, out = poc_job(f"vscode_{key}", {}, sbatch, timeout_min=10)
        want = re.escape(key) if key != "current" else r"[\d.]+"
        ok = (re.search(rf"^code: code {want}\b", out, re.M) and re.search(r"^tunnel: \S", out, re.M)
              and "slurm: slurm 20.11" in out and "slurm: export=NONE" in out)
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
    keep = r"^(node=|listening|sign_in|do_sign_in|ide:|rsession:|imports:|login|api_status|lab:|kernel|image=|code:|tunnel:|slurm:)"
    return "\n".join(l[:160] for l in out.splitlines() if re.match(keep, l))


def release(plugin, rel):
    branch = f"release-bot/{plugin.app}-{rel.key}"
    if remote_branch_exists(branch):
        log(f"[{plugin.app}] {branch} already exists (PR pending); skipping")
        return
    check_disk(plugin.app)
    ctx = plugin.build_context() if hasattr(plugin, "build_context") else REPO / plugin.app
    sif = STATE / "build" / rel.image
    versions = apptainer_build(sif, REPO / plugin.app / rel.deffile, rel.build_args, ctx)
    log(f"image versions: {versions}")
    sha = upload(sif, rel.image)
    sif.unlink()

    run(["git", "checkout", "-q", "-b", branch], cwd=REPO)
    plugin.patch(rel)
    run(["git", "-c", "user.name=ood release bot", "commit", "-qam",
         f"{plugin.title}: add {rel.summary}\n\nImage {IMAGES}/{rel.image} (sha256 {sha}), built by "
         f".github/release-bot from {plugin.app}/{rel.deffile} and tested on ood-poc."], cwd=REPO)
    run(["git", "push", "-q", "-u", "origin", branch], cwd=REPO)

    publish_to_poc(branch)
    ok, job, out = plugin.poc_test(rel.key)
    image_versions = "\n".join(f"{k}={v}" for k, v in versions.items())
    if not ok:
        raise RuntimeError(f"ood-poc test of {rel.summary} failed (POC job {job}).\nBranch {branch} and image "
                           f"{IMAGES}/{rel.image} were left in place.\n\n{out}")
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
    a = ap.parse_args()
    if a.ref != "master" and not (a.dry_run or a.test_existing):
        sys.exit("--ref is only for --dry-run and --test-existing; releases always start from master")

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
            publish_to_poc(a.ref)
            ok, job, out = plugin.poc_test(key)
        except Exception as e:
            notify(f"release bot: ood-poc test of {app}:{key} FAILED", f"{e}\n\n{traceback.format_exc()}")
            raise
        log(f"ood-poc test of {app}:{key}: {'passed' if ok else 'FAILED'}\n{summary_of(out)}")
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
                    release(plugin, rel)
                    checkout("master")
        except Exception as e:
            failed = True
            log(f"[{name}] FAILED: {e}")
            notify(f"release bot: {plugin.title} FAILED",
                   f"{e}\n\nManual step: read the log, fix the cause, then re-run\n"
                   f"  systemctl --user start ood-release-bot.service\n\n{traceback.format_exc()[-3000:]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
