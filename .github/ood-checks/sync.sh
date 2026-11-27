#!/bin/sh
# Copy the shared start-up checks, the session-card view and the Slurm helper into the apps.
set -eu
cd "$(dirname "$0")/../.."
for app in rstudio_bioconductor_321 jupyter vscode_cli_tunnel igv juicebox cellwishperer_web; do
  cp .github/ood-checks/ood_checks.sh "$app/template/ood_checks.sh"
  cp .github/ood-checks/completed.md.erb "$app/completed.md.erb"
done
# Slurm in the containers of the apps with a terminal.
for app in rstudio_bioconductor_321 jupyter vscode_cli_tunnel igv; do
  cp .github/ood-checks/ood_slurm.sh "$app/template/ood_slurm.sh"
done
