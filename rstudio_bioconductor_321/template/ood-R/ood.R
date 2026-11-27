# OnDemand helpers for R, attached as "ood" by ood-R/Rprofile.site.
#
#   ood_reinstall_packages()                 # reinstall what a previous library had
#   ood_reinstall_packages(dry_run = TRUE)   # only show what it would do
#   ood_reinstall_packages(from = "3.21")    # from a specific previous version
#   ood_previous_libraries()                 # the libraries it can reinstall from
#
# Each Bioconductor version has its own library (packages are built for one R
# and one Bioconductor release, and mixing releases breaks BiocManager), so a
# new version starts empty. This installs the packages of a previous library
# into the current one: CRAN and Bioconductor packages as binaries where
# Posit's package manager has them (seconds each, instead of compiling),
# GitHub packages from the repository and commit they were installed from.
# It installs the versions that belong to this Bioconductor release, not the
# old ones.

ood_previous_libraries <- function() {
  home <- path.expand("~")
  cands <- c(Sys.glob(file.path(home, "rstudio_server*", "R", "user-libs")),
             Sys.glob(file.path(home, "R", "*", "*")))
  cands <- unique(normalizePath(cands[dir.exists(cands)], mustWork = FALSE))
  current <- normalizePath(.libPaths()[1], mustWork = FALSE)
  cands <- setdiff(cands, current)
  rows <- lapply(cands, function(lib) {
    pk <- list.dirs(lib, recursive = FALSE, full.names = FALSE)
    pk <- pk[file.exists(file.path(lib, pk, "DESCRIPTION"))]
    if (!length(pk)) return(NULL)
    built <- vapply(file.path(lib, pk, "DESCRIPTION"), function(f) {
      b <- tryCatch(read.dcf(f, fields = "Built")[1, 1], error = function(e) NA)
      sub("^R ([0-9.]+);.*", "\\1", b)
    }, "")
    data.frame(library = lib, packages = length(pk),
               r_version = names(sort(table(built), decreasing = TRUE))[1],
               modified = max(file.mtime(file.path(lib, pk))), stringsAsFactors = FALSE)
  })
  out <- do.call(rbind, rows)
  if (is.null(out)) return(data.frame(library = character(), packages = integer()))
  out[order(out$modified, decreasing = TRUE), , drop = FALSE]
}

.ood_pick_library <- function(from) {
  libs <- ood_previous_libraries()
  if (!nrow(libs)) stop("no previous R library with packages found in your home folder", call. = FALSE)
  if (is.null(from)) return(libs$library[1])
  if (dir.exists(from)) return(normalizePath(from))
  # A version: "3.21" -> rstudio_server_bioconductor_3.21 / ..._tidyverse_321, or an R version "4.4".
  key <- gsub(".", "", from, fixed = TRUE)
  hit <- libs$library[grepl(paste0("(_|/)(", gsub(".", "\\.", from, fixed = TRUE), "|", key, ")(/|$)"), libs$library)]
  if (length(hit) != 1)
    stop(sprintf("%d previous libraries match %s; pass the folder, one of:\n  %s", length(hit), from,
                 paste(libs$library, collapse = "\n  ")), call. = FALSE)
  hit
}

ood_reinstall_packages <- function(from = NULL, ncpus = NULL, dry_run = FALSE, ask = interactive()) {
  lib <- .ood_pick_library(from)
  target <- .libPaths()[1]
  if (is.null(ncpus)) ncpus <- max(1L, as.integer(Sys.getenv("SLURM_CPUS_PER_TASK", parallel::detectCores())), na.rm = TRUE)
  fields <- c("Repository", "RemoteType", "RemoteUsername", "RemoteRepo", "RemoteRef", "RemoteSha", "RemoteHost", "biocViews")
  old <- utils::installed.packages(lib.loc = lib, fields = fields, noCache = TRUE)
  old <- old[is.na(old[, "Priority"]) | !old[, "Priority"] %in% c("base", "recommended"), , drop = FALSE]
  have <- rownames(utils::installed.packages(noCache = TRUE))
  todo <- old[!rownames(old) %in% have, , drop = FALSE]
  remote <- !is.na(todo[, "RemoteType"]) & todo[, "RemoteType"] %in% c("github", "gitlab", "bitbucket")
  origin <- ifelse(remote, todo[, "RemoteType"],
            ifelse(grepl("^Bioconductor", todo[, "Repository"]) | !is.na(todo[, "biocViews"]), "Bioconductor",
            ifelse(is.na(todo[, "Repository"]), "unknown", "CRAN")))
  plan <- data.frame(package = rownames(todo), origin = origin, old_version = todo[, "Version"],
                     status = "to install", stringsAsFactors = FALSE, row.names = NULL)
  if (!nrow(plan)) {
    message(sprintf("Nothing to do: all %d packages of %s are already available.", nrow(old), lib))
    return(invisible(plan))
  }
  repos <- BiocManager::repositories()
  avail <- rownames(utils::available.packages(repos = repos))
  from_repos <- plan$origin != "github" & plan$origin != "gitlab" & plan$origin != "bitbucket"
  plan$status[from_repos & !plan$package %in% avail] <- "not in this release"
  message(sprintf(paste0("From %s (%d packages) into %s:\n",
                         "  %d from CRAN/Bioconductor (binaries where available), %d from GitHub/GitLab,\n",
                         "  %d no longer available in Bioconductor %s / this CRAN snapshot, %d already here."),
                  lib, nrow(old), target, sum(plan$status == "to install" & from_repos), sum(!from_repos),
                  sum(plan$status == "not in this release"), as.character(BiocManager::version()), nrow(old) - nrow(plan)))
  if (any(plan$status == "not in this release"))
    message("  Not available: ", paste(plan$package[plan$status == "not in this release"], collapse = ", "))
  if (dry_run) return(invisible(plan))
  if (ask && !isTRUE(utils::askYesNo("Install them now?"))) return(invisible(plan))

  pk <- plan$package[plan$status == "to install" & from_repos]
  if (length(pk)) {
    BiocManager::install(pk, update = FALSE, ask = FALSE, Ncpus = ncpus, lib = target)
  }
  for (i in which(!from_repos)) {
    r <- todo[plan$package[i], ]
    spec <- paste0(r[["RemoteUsername"]], "/", r[["RemoteRepo"]],
                   if (!is.na(r[["RemoteSha"]])) paste0("@", r[["RemoteSha"]]) else if (!is.na(r[["RemoteRef"]])) paste0("@", r[["RemoteRef"]]) else "")
    fun <- switch(plan$origin[i], github = remotes::install_github, gitlab = remotes::install_gitlab, remotes::install_bitbucket)
    tryCatch(fun(spec, upgrade = "never", lib = target, dependencies = NA),
             error = function(e) message("  ", plan$package[i], ": ", conditionMessage(e)))
  }
  now <- rownames(utils::installed.packages(lib.loc = target, noCache = TRUE))
  todo_rows <- plan$status == "to install"
  plan$status[todo_rows] <- ifelse(plan$package[todo_rows] %in% now, "installed", "failed")
  log <- file.path(target, sprintf(".ood-reinstall-%s.csv", format(Sys.time(), "%Y%m%d-%H%M%S")))
  try(utils::write.csv(plan, log, row.names = FALSE), silent = TRUE)
  message(sprintf("Installed %d, failed %d, not available %d. Details: %s",
                  sum(plan$status == "installed"), sum(plan$status == "failed"),
                  sum(plan$status == "not in this release"), log))
  if (any(plan$status == "failed"))
    message("  Failed: ", paste(plan$package[plan$status == "failed"], collapse = ", "),
            "\n  The messages above say why. A missing system library (\"cannot open shared object file\",",
            "\n  \"configuration failed\") has to be added to the image: send the package name to the HPC team.")
  invisible(plan)
}

# Shown once per library, at the start of an interactive session whose
# library is still empty while a previous one has packages.
.ood_reinstall_hint <- function() {
  target <- .libPaths()[1]
  marker <- file.path(target, ".ood-reinstall-hint-shown")
  if (!dir.exists(target) || file.exists(marker)) return(invisible())
  if (length(list.dirs(target, recursive = FALSE)) > 0) return(invisible())
  libs <- tryCatch(ood_previous_libraries(), error = function(e) NULL)
  if (is.null(libs) || !nrow(libs)) return(invisible())
  message(sprintf(paste0("\nThis R/Bioconductor version has its own, still empty, package library.\n",
                         "%d packages are in %s. To install them here (binaries where available):\n",
                         "  ood_reinstall_packages(dry_run = TRUE)   # see what it would do\n",
                         "  ood_reinstall_packages()                 # install them\n"),
                  libs$packages[1], libs$library[1]))
  try(file.create(marker), silent = TRUE)
  invisible()
}
