from __future__ import annotations

import importlib.machinery
import shutil
import subprocess
import sys
from pathlib import Path


def _is_python_extension_module(path: Path) -> bool:
    """True if *path* is a real file whose name matches ``EXTENSION_SUFFIXES``."""
    if path.is_symlink():
        return False
    return any(
        path.name.endswith(suf) for suf in importlib.machinery.EXTENSION_SUFFIXES
    )


def _package_dirs_with_native_extensions(staging_dir: Path) -> set[Path]:
    """Parent dirs of each Python extension module under *staging_dir*."""
    package_dirs: set[Path] = set()
    for pattern in ("*.so", "*.pyd"):
        for path in staging_dir.rglob(pattern):
            if not path.is_file():
                continue
            if _is_python_extension_module(path):
                package_dirs.add(path.parent)
    return package_dirs


def move_deploy_to_wheel(deploy_folder: Path, staging_dir: Path) -> None:
    """Merge ``runtime_deploy`` into each package dir that has a native extension."""
    if not deploy_folder.is_dir() or not any(deploy_folder.iterdir()):
        return

    for pkg_dir in _package_dirs_with_native_extensions(staging_dir):
        shutil.copytree(deploy_folder, pkg_dir, dirs_exist_ok=True)


def patch_rpath(staging_dir: Path) -> None:
    """Add $ORIGIN / @loader_path RPATH to all .so/.dylib files in staging_dir."""
    if sys.platform == "linux":
        _patch_rpath_linux(staging_dir)
    elif sys.platform == "darwin":
        _patch_rpath_darwin(staging_dir)


def _run_silent(cmd: list[str], warned: list[bool]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        if not warned[0]:
            tool = cmd[0]
            print(
                f"WARNING: {tool} not found. Shared libs may not load correctly. "
                f"Install {tool} for proper RPATH patching.",
                flush=True,
            )
            warned[0] = True
    except subprocess.CalledProcessError:
        pass  # RPATH may already be set


def _patch_rpath_linux(staging_dir: Path) -> None:
    warned = [False]
    for path in staging_dir.rglob("*.so"):
        if not path.is_file() or path.is_symlink():
            continue
        if _is_python_extension_module(path):
            rpath = "$ORIGIN:$ORIGIN/lib"
        else:
            rpath = "$ORIGIN"
        _run_silent(["patchelf", "--add-rpath", rpath, str(path)], warned)


def _patch_rpath_darwin(staging_dir: Path) -> None:
    warned = [False]
    for pattern in ("*.so", "*.dylib"):
        for path in staging_dir.rglob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            _run_silent(["install_name_tool", "-add_rpath", "@loader_path", str(path)], warned)


