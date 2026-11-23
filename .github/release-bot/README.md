# Release bot

`release_bot.py` keeps three apps up to date with their upstream releases.
It goes as far as opening a pull request; a maintainer merges it.

| App | Watches | Each new release becomes |
|---|---|---|
| Bioconductor (`rstudio_bioconductor_321`) | `bioconductor/tidyverse` `X.Y-R-a.b.c` tags (April/October) | `bioconductor_X.Y.sif` (latest stable RStudio Server), a new entry in the version select |
| Jupyter Lab (`jupyter`) | JupyterLab and PyTorch minor releases on PyPI (PyTorch only once its torchvision exists) | `jupyter-cellranger_YYYY.MM.sif`, a new entry in the image select |
| VS Code Tunnel (`vscode_cli_tunnel`) | VS Code stable releases | `vscode-tunnel_<version>.sif`; the app switches to it |

For each new release it:

1. builds the image from the app's `.def`, then uploads it to
   `/ijc/PUBLIC/DATA/images`. It never overwrites an existing file;
2. adds the release to the app on the branch `release-bot/<app>-<version>`,
   which starts from `master`. Form defaults are never changed;
3. **tests the release on ood-poc**:
   - publishes the branch to the POC portal (the `deploy-ood-poc.yml`
     workflow in OnDemandDocker);
   - reads the job script as the POC portal serves it, where the cluster
     and partition are rewritten for the POC cluster;
   - renders that script and runs it as a Slurm job on the POC cluster
     (`hpc-new`);
   - checks the running app:

   | App | Checks |
   |---|---|
   | RStudio | sign-in, the IDE page, and an R session started with the right library |
   | Jupyter | login, the API, the Lab page, a Python kernel reaching idle, and that PyTorch imports |
   | VS Code | the CLI version, the tunnel CLI, and the Slurm client in the image (the tunnel itself needs an interactive Microsoft login) |
4. if the test passes, opens a PR to `master` and emails the maintainer.
   Only that merge reaches production; it publishes in place, without a
   restart.

The POC cluster has no GPU partition, so GPU paths are not tested there.

**Manual steps (each one sends an email):**

- **"... is ready - review and merge"**: review the PR and merge it. ood-poc
  still serves the branch, so you can try it there first.
- **"... FAILED"**: read the log named in the email, fix the cause, then run
  `systemctl --user start ood-release-bot.service`. After a failure, the
  branch and the uploaded image stay in place. Delete them before retrying
  the same version.

## Running it

It runs on a maintainer workstation as a daily systemd user timer, not as a
GitHub Actions runner. This repo is public, and a self-hosted runner on a
public repo can run code from forks' pull requests.

The workstation needs:

- `apptainer` (with `--fakeroot`) and `docker`, which renders the ERB
  templates with `ruby:3-slim`;
- about 45 GB of free disk for a Jupyter build;
- passwordless SSH to the cluster login host (uploads, the POC jump host,
  mail) and to the kubectl host (it checks what ood-poc serves);
- `gh` logged in with push rights on this repo, and permission to run
  workflows on OnDemandDocker. Its token lives in the desktop keyring, so a
  run fails, and emails, while nobody is logged in.

`~/.config/ood-release-bot/env` holds the config. It is not in git, because
the repo is public:

```sh
NOTIFY_EMAIL=you@example.org
SSH_HOST=minastirith
POC_HOST=hpclogin-new
POC_PARTITION=hpc-new
KUBE_HOST=super@maverick
```

The unit files go in `~/.config/systemd/user/`. Each run updates a separate
clone of `master` and runs the bot from it, so bot changes go live once they
are merged:

```ini
# ood-release-bot.service
[Service]
Type=oneshot
ExecStartPre=/bin/sh -c 'd=%h/.cache/ood-release-bot/bot; test -d $d/.git || git clone -q git@github.com:carrerasresearch/onDemand.git $d; git -C $d fetch -q origin && git -C $d checkout -q -B master origin/master'
ExecStart=%h/.cache/ood-release-bot/bot/.github/release-bot/release_bot.py
TimeoutStartSec=8h

# ood-release-bot.timer
[Timer]
OnCalendar=*-*-* 07:30
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
```

Then enable it with `systemctl --user enable --now ood-release-bot.timer`.

Manual runs:

```sh
release_bot.py --dry-run                                   # what is new upstream
release_bot.py --only jupyter                              # one app
release_bot.py --test-existing jupyter:cuda13 [--ref BR]   # POC test of an offered version; changes nothing
```

Never add a broad `/tmp` cleanup to a `.def`. During `apptainer build`,
`/tmp` is the host's `/tmp`.
