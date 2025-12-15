# run.py
# Simple, robust launcher for your bot on Railway.

import os
import sys
import runpy

def _diag_and_exit():
    root = os.path.abspath(os.path.dirname(__file__))
    try:
        listing = ", ".join(sorted(os.listdir(root)))
    except Exception as e:
        listing = f"<listdir failed: {e}>"
    print("========== LAUNCH ERROR ==========")
    print(f"CWD: {os.getcwd()}")
    print(f"ROOT: {root}")
    print(f"Top-level entries: {listing}")
    print("Could not locate the main module.")
    print("Expected: Feral_Kitty_FiFi/main.py inside the Feral_Kitty_FiFi/ package.")
    print("Start this app with: python run.py")
    print("==================================")
    raise SystemExit(1)

if __name__ == "__main__":
    # Ensure repo root on sys.path
    sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

    for mod in ("Feral_Kitty_FiFi.main", "feral_kitty_fifi.main"):
        try:
            # Runs as if `python -m <mod>` (executes top-level code)
            runpy.run_module(mod, run_name="__main__", alter_sys=True)
            raise SystemExit(0)
        except ModuleNotFoundError:
            continue
        except SystemExit:
            raise
        except Exception as e:
            # Surface real errors from your main quickly
            print(f"Error while running {mod}: {type(e).__name__}: {e}")
            raise

    _diag_and_exit()
