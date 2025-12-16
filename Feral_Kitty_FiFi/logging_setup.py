# Feral_Kitty_FiFi/logging_setup.py
import logging
import sys

def init_logging(level: int = logging.INFO) -> None:
    """
    Configure root logger once for simple console logs.
    """
    if logging.getLogger().handlers:
        return  # already configured

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
