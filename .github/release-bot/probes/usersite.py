"""Check that the user's pip --user folder cannot override the image's packages.

Run inside a Jupyter image, in jlab-env. Images with the OnDemand
sitecustomize.py put the user's folder (PYTHONUSERBASE) after the image's
packages. This puts a broken "numpy" and a package the image lacks in a
throwaway user folder: the image's numpy must win, and the other package
must still import. Prints "usersite: ok", "usersite: not protected (older
image)" or "usersite: FAILED ...".
"""
import os
import subprocess
import sys
import sysconfig
import tempfile

if not os.path.exists(os.path.join(sysconfig.get_paths()["purelib"], "sitecustomize.py")):
    print("usersite: not protected (older image)")
    sys.exit(0)

base = tempfile.mkdtemp(prefix="usersite-", dir=".")
env = dict(os.environ, PYTHONUSERBASE=os.path.abspath(base))
env.pop("PYTHONNOUSERSITE", None)
user_site = subprocess.check_output([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                                    env=env, text=True).strip()
os.makedirs(os.path.join(user_site, "numpy"))
with open(os.path.join(user_site, "numpy", "__init__.py"), "w") as f:
    f.write('raise ImportError("stale numpy from the user folder was imported")\n')
with open(os.path.join(user_site, "ood_user_only.py"), "w") as f:
    f.write("OK = True\n")
p = subprocess.run([sys.executable, "-c", "import numpy, ood_user_only; print(numpy.__version__, ood_user_only.OK)"],
                   env=env, capture_output=True, text=True)
if p.returncode == 0 and p.stdout.split()[-1:] == ["True"]:
    print(f"usersite: ok (image numpy {p.stdout.split()[0]} wins, user-only packages still load)")
else:
    print("usersite: FAILED", (p.stdout + p.stderr).strip().splitlines()[-1:])
