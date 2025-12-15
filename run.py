# run.py — smart launcher
# Works whether your code sits at /app or in /app/Feral_Kitty_FiFi/

import os, sys, types, importlib.util

ROOT = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

def exists(p): return os.path.exists(p)

# 1) If a proper package exists, try normal import first
for pkg in ("Feral_Kitty_FiFi", "feral_kitty_fifi"):
    try:
        __import__(f"{pkg}.main")
        raise SystemExit(0)
    except ModuleNotFoundError:
        pass

# 2) If main.py is INSIDE a package folder, load it by path
for pkg in ("Feral_Kitty_FiFi", "feral_kitty_fifi"):
    pkg_dir = os.path.join(ROOT, pkg)
    main_py = os.path.join(pkg_dir, "main.py")
    if exists(main_py):
        spec = importlib.util.spec_from_file_location(f"{pkg}.main", main_py)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader, "invalid import spec for package main"
        spec.loader.exec_module(mod)  # should call bot.run(...)
        raise SystemExit(0)

# 3) Last resort: your main.py is at repo root. Synthesize a package so relative imports work.
root_main = os.path.join(ROOT, "main.py")
if exists(root_main):
    pkg_name = "Feral_Kitty_FiFi"
    # Make a fake package that points to ROOT so relative imports in main.py resolve.
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]  # mark as package
    sys.modules[pkg_name] = pkg

    # Load main.py as Feral_Kitty_FiFi.main
    spec = importlib.util.spec_from_file_location(f"{pkg_name}.main", root_main)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader, "invalid import spec for root main"
    spec.loader.exec_module(mod)  # should call bot.run(...)
    raise SystemExit(0)

# 4) If nothing worked, print diagnostics
print("========== LAUNCH DIAGNOSTICS ==========")
print("CWD:", os.getcwd())
print("ROOT:", ROOT)
try:
    print("Top-level entries:", os.listdir(ROOT))
except Exception as e:
    print("listdir failed:", e)
print("Could not find main.py. Expected one of:")
print(" - /app/Feral_Kitty_FiFi/main.py")
print(" - /app/feral_kitty_fifi/main.py")
print(" - /app/main.py")
print("========================================")
raise SystemExit(1)
