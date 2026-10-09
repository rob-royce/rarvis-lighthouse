"""Exact launcher bindings shared by worker guidance and hook authorization."""
from pathlib import Path
import shlex
import sys


def worker_prefixes(home: Path) -> tuple[tuple[str, ...], ...]:
    """Return only launchers belonging to this source tree or installed interpreter."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and all(
            (root / "bin" / name).is_file() for name in ("rlh", "gcl")):
        launchers = ((str(root / "bin/rlh"),), (str(root / "bin/gcl"),))
    else:
        # Keep the venv invocation path: resolving its symlink can escape the venv.
        # Isolated mode excludes the worker's cwd and inherited PYTHONPATH.
        if not sys.executable or not Path(sys.executable).is_absolute():
            raise ValueError("worker interpreter must be an absolute path")
        # -I also hides user-site installs. Add only this package's trusted root,
        # not the worker's inherited import paths, before loading the entry point.
        bootstrap = (f"import sys; sys.path.insert(0, {str(root)!r}); "
                     "from gcl.cli import main; raise SystemExit(main())")
        launcher = (sys.executable, "-I", "-B", "-c", bootstrap)
        launchers = (launcher, launcher)
    return tuple((f"{variable}={home}", *launcher)
                 for variable, launcher in zip(("RLH_HOME", "GCL_HOME"), launchers))


def worker_prefix(home: Path) -> str:
    """Render the primary binding as one shell assignment plus a quoted argv."""
    assignment, *argv = worker_prefixes(home)[0]
    variable, value = assignment.split("=", 1)
    return f"{variable}={shlex.quote(value)} {shlex.join(argv)}"
