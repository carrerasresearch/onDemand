#!/bin/sh
# Copy the shared start-up checks and the session-card view into every app.
set -eu
cd "$(dirname "$0")/../.."
for app in rstudio_bioconductor_321 jupyter vscode_cli_tunnel igv juicebox cellwishperer_web; do
  cp .github/ood-checks/ood_checks.sh "$app/template/ood_checks.sh"
  cp .github/ood-checks/completed.md.erb "$app/completed.md.erb"
done
