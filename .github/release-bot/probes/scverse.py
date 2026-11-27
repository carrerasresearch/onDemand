"""Check the scverse stack in a Jupyter image (run inside it, in jlab-env).

Prints one line starting with "scverse:": "not in image" for images built
before the stack was added, "ok ..." when a small synthetic dataset goes
through scanpy (PCA, neighbours, Leiden, UMAP), decoupler and a LIANA+
consensus ranking, otherwise "FAILED" and the traceback. Needs no network.
"""
import importlib.metadata
import importlib.util
import sys
import time
import traceback

if importlib.util.find_spec("liana") is None:
    print("scverse: not in image")
    sys.exit(0)

t0 = time.time()
try:
    import anndata as ad
    import decoupler as dc
    import liana as li
    import mudata  # noqa: F401
    import muon  # noqa: F401
    import numpy as np
    import pandas as pd
    import scanpy as sc
    import squidpy  # noqa: F401

    rng = np.random.default_rng(0)
    # Genes from LIANA's bundled consensus resource (complexes are "A_B").
    res = li.resource.select_resource("consensus")
    genes = sorted({g for col in ("ligand", "receptor") for x in res[col] for g in x.split("_")})[:600]
    n, groups = 300, np.array(["A", "B", "C"])
    ct = groups[np.arange(n) % 3]
    rate = rng.gamma(0.5, 2, size=(3, len(genes)))      # each cell type its own profile
    counts = rng.poisson(rate[np.arange(n) % 3]).astype(np.float32)
    adata = ad.AnnData(counts, obs=pd.DataFrame({"ct": pd.Categorical(ct)}, index=[f"c{i}" for i in range(n)]),
                       var=pd.DataFrame(index=genes))
    sc.pp.normalize_total(adata)
    sc.pp.log1p(adata)
    sc.pp.pca(adata, n_comps=20)
    sc.pp.neighbors(adata)
    sc.tl.leiden(adata, flavor="igraph", n_iterations=2)
    sc.tl.umap(adata)

    net = pd.DataFrame({"source": ["S1"] * 20 + ["S2"] * 20, "target": genes[:40], "weight": 1.0})
    dc.mt.ulm(adata, net, tmin=5, verbose=False)
    scores = adata.obsm["score_ulm"]

    li.mt.rank_aggregate(adata, groupby="ct", resource_name="consensus", expr_prop=0.05,
                         use_raw=False, n_perms=20, verbose=False)
    lr = adata.uns["liana_res"]
    if lr.empty:
        raise RuntimeError("LIANA+ returned no interactions")
    ver = importlib.metadata.version
    print(f"scverse: ok scanpy {ver('scanpy')}, liana {ver('liana')}: {len(lr)} interactions, "
          f"{adata.obs['leiden'].nunique()} leiden clusters, ulm {scores.shape[1]} sources, "
          f"{time.time() - t0:.0f}s")
except Exception:
    print("scverse: FAILED")
    traceback.print_exc(file=sys.stdout)
