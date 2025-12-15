# run.py  — resilient launcher for Feral_Kitty_FiFi
# Runs even if your package path/case is slightly off, and gives clear diagnostics.

import os, sys, importlib.util

ROOT = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT)  # ensure repo root is importable

def _exists(p): return os.path.exists(p)
def _die(msg):
    # Print strong diagnostics to logs, then exit with non-zero code
    print("========== LAUNCH DIAGNOSTICS ==========")
    print(f"CWD: {os.getcwd()}")
    print(f"ROOT: {ROOT}")
    try:
        print("Top-level entries:", os.listdir(ROOT))
    except Exception as e:
        print("listdir failed:", e)
    print(msg)
    print("========================================")
    raise SystemExit(1)

# Candidate package/folder names to try (exact case first)
CANDIDATE_PKGS = [
    "Feral_Kitty_FiFi",     # what you intended
    "feral_kitty_fifi",     # lowercase fallback
]

# 1) Try clean import "pkg.main"
for pkg in CANDIDATE_PKGS:
    try:
        __import__(f"{pkg}.main")
        # If import succeeds, we're done; side-effects will start the bot.
        # (discord bot run() is in main module body)
        raise SystemExit(0)
    except ModuleNotFoundError:
        pass  # try next strategy

# 2) Try running main.py by absolute file path if the folder exists
for pkg in CANDIDATE_PKGS:
    pkg_dir = os.path.join(ROOT, pkg)
    main_py = os.path.join(pkg_dir, "main.py")
    init_py = os.path.join(pkg_dir, "__init__.py")
    if _exists(main_py):
        # If __init__.py is missing, add ROOT to path and load main.py directly
        try:
            spec = importlib.util.spec_from_file_location(f"{pkg}.main", main_py)
            mod = importlib.util.module_from_spec(spec)
            assert spec and spec.loader, "invalid import spec"
            spec.loader.exec_module(mod)  # this should run bot.run(...)
            raise SystemExit(0)
        except Exception as e:
            _die(f"Failed to exec {main_py}: {type(e).__name__}: {e}")

# 3) Last resort: maybe you put main.py at the repo root (no package)
root_main = os.path.join(ROOT, "main.py")
if _exists(root_main):
    try:
        spec = importlib.util.spec_from_file_location("main", root_main)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader, "invalid import spec"
        spec.loader.exec_module(mod)
        raise SystemExit(0)
    except Exception as e:
        _die(f"Failed to exec {root_main}: {type(e).__name__}: {e}")

# If we get here, show a helpful error with likely fixes
_hints = [
    "1) Ensure the folder exists: /app/Feral_Kitty_FiFi/",
    "2) Ensure files exist: /app/Feral_Kitty_FiFi/__init__.py and /app/Feral_Kitty_FiFi/main.py",
    "3) Start command in Railway: python run.py",
    "4) Folder name is CASE-SENSITIVE. Must match exactly: Feral_Kitty_FiFi",
    "5) If your folder is lowercase, the launcher will try that too (feral_kitty_fifi).",
]
_die("Could not locate your main module. Hints:\n- " + "\n- ".join(_hints))
