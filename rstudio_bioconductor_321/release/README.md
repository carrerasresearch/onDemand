# Bioconductor release checker

`bioc-release.sh` adds new Bioconductor releases to the "Bioconductor" app
up to the point of a pull request. Bioconductor publishes a release twice a
year, in April and October.

A daily run does nothing until `bioconductor/tidyverse` publishes an
`X.Y-R-a.b.c` tag that the app doesn't offer yet. When it finds one, it:

1. builds `bioconductor_X.Y.sif` from `../rstudio_bioconductor.def`, using the
   latest stable RStudio Server;
2. uploads the image to `/ijc/PUBLIC/DATA/images`. It never overwrites an
   existing file;
3. adds `X.Y` to `form.yml` and `template/script.sh.erb` on the branch
   `bioc-release-X.Y`. The default version is left unchanged;
4. runs the app's job script on a `test2` node, using `probe.py`, and checks
   that rserver starts, sign-in works, the IDE page loads and an R session
   starts with the right library;
5. opens a pull request and emails the maintainer.

You need to step in in two cases, and both send an email:

- **"ready: review and merge"**: merge the PR. A merge publishes the app on
  the production portal in place, without a restart.
- **"FAILED"**: read the log named in the email, fix the cause, then run
  `systemctl --user start bioc-release.service`.

## Where it runs

It runs on a maintainer's workstation as a systemd user timer, not as a
GitHub Actions runner. The repo is public, and a self-hosted runner on a
public repo can run code from forks' pull requests.

The workstation needs:

- `apptainer` (with `--fakeroot`) and `docker`, which renders the ERB with
  `ruby:3-slim`;
- passwordless SSH to the cluster login host, which submits the test job and
  sends the mail;
- `gh`, logged in with push rights. Its token lives in the desktop keyring, so
  the run fails, and emails, while nobody is logged in.

Setup:

```sh
mkdir -p ~/.config/bioc-release
cat > ~/.config/bioc-release/env <<'EOF'
NOTIFY_EMAIL=you@example.org
SSH_HOST=minastirith
PARTITION=test2
EOF
# the bioc-release.service/.timer unit files are in ~/.config/systemd/user (see below)
systemctl --user enable --now bioc-release.timer
```

Manual runs:

```sh
./bioc-release.sh --dry-run             # what would happen
./bioc-release.sh --test-existing 3.23  # test, upload and email path only; changes nothing
```

The service checks out `master` before each run, so changes to this script go
live once they're merged.

Unit files:

```ini
# ~/.config/systemd/user/bioc-release.service
[Service]
Type=oneshot
ExecStartPre=/bin/sh -c 'test -d %h/.cache/bioc-release/repo/.git || git clone -q git@github.com:carrerasresearch/onDemand.git %h/.cache/bioc-release/repo; git -C %h/.cache/bioc-release/repo fetch -q origin && git -C %h/.cache/bioc-release/repo checkout -q -B master origin/master'
ExecStart=%h/.cache/bioc-release/repo/rstudio_bioconductor_321/release/bioc-release.sh
TimeoutStartSec=3h

# ~/.config/systemd/user/bioc-release.timer
[Timer]
OnCalendar=*-*-* 07:30
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
```

Never add a broad `/tmp` cleanup to the `.def`. During `apptainer build`,
`/tmp` is the host's `/tmp`.
