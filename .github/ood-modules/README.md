# The OnDemand app environments in Slurm jobs

The images of the OnDemand **Jupyter Lab** and **RStudio** apps can be used in
Slurm jobs and interactive shells as modules. You get the same software, set up
the same way as in the app: the same Python packages and conda environments,
and the same R library for each Bioconductor version.

```bash
module use @DEST@/modulefiles     # once per shell (or in ~/.bashrc)
module avail ood                  # ood/jupyter/<version>, ood/rstudio/<version>, ood/conda
module load ood/jupyter           # the newest image; or e.g. ood/jupyter/2026.11.2
module help ood/jupyter           # what is in it, and examples
```

Containers do not run on the login node, so submit the work as a job:

```bash
ood-run -c 8 --mem 32G -t 4:00:00 -- python analysis.py   # a batch job (log: ood-<jobid>.out)
ood-run --gpu -c 4 --mem 32G -- python train.py           # on a GPU node, GPU passed through
ood-run --print -c 8 -- python analysis.py > job.sh       # only write the job script
ood-shell -c 4 --mem 16G -t 2:00:00                       # an interactive shell in the image
```

Inside a job (an `sbatch` script, `srun`, `salloc`), the module's commands run
in the image directly:

```bash
#!/bin/bash
#SBATCH -c 8 --mem 32G -t 4:00:00
module use @DEST@/modulefiles
module load ood/jupyter/2026.11.2
python analysis.py               # also: pip, jupyter, cellranger, cellranger-arc, conda, uv
OOD_CONDA_ENV=myenv python x.py  # a conda env of ~/.conda/envs instead of the image's Python
ood-exec bash -c '...'           # anything else, inside the image
```

For RStudio, `module load ood/rstudio/3.24` gives `R` and `Rscript` with the
RStudio app's library for that version. A new Bioconductor version starts
with an empty library. To install the packages of a previous version there
(binaries where available):

```bash
ood-run -c 16 --mem 32G -- Rscript -e 'ood_reinstall_packages(ask = FALSE)'
```

## From an app's terminal

In the terminals of the Jupyter, RStudio, VS Code and IGV apps, `sbatch`,
`srun`, `squeue`, ... are the cluster's own commands. Jobs you submit there
start like jobs submitted from a cluster shell, without the app's container,
and `module load` works in their scripts. `ood-run` and `ood-shell` also work
there, using the session's own image. `ood-host` runs a command, or opens a
shell, on the session's node outside the container, within the session's job.

## Conda environments

Conda environments in `~/.conda/envs` work everywhere: in every Jupyter image
(as kernels, automatically), in VS Code, and in Slurm jobs:

```bash
module load ood/conda && conda activate myenv    # in a job script or a cluster shell
~/.conda/envs/myenv/bin/python script.py         # or directly
```

Run `module purge` before that, so that other modules' libraries do not mix
with the env's.

*Published by `.github/ood-modules/publish.py` from the onDemand repository;
the release bot republishes when images change. Do not edit files here.*
