"""Repo2RLEnv — turn any repository into an RL environment for training and evaluation."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single source of truth: read whatever pyproject.toml declared at install time.
    __version__ = _pkg_version("repo2rlenv")
except PackageNotFoundError:
    # Running from source without `pip install -e .` — rare, but don't crash.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
