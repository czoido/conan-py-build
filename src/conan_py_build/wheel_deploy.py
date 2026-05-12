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


def _is_shared_library(path: Path) -> bool:
    """True if *path* is a real shared library we should patch (not a symlink)."""
    if path.is_symlink() or not path.is_file():
        return False
    if sys.platform == "darwin":
        return path.suffix == ".dylib"
    if sys.platform == "linux":
        # Match .so and versioned forms like libfoo.so.2 or libfoo.so.2.6.1.
        return ".so" in path.suffixes
    return False


def patch_rpath(staging_dir: Path) -> None:
    """Add ``@loader_path`` / ``$ORIGIN`` rpath to every shared lib that sits
    next to a Python extension.

    Patching only the extension modules is not enough on Linux: ``DT_RUNPATH``
    (the GNU default since glibc 2.30) is not inherited across transitive
    library loads. If ``_ext.so`` has rpath ``$ORIGIN`` and loads
    ``libfoo.so`` (bundled next to it), ``libfoo``'s own deps will not be
    looked up via ``_ext``'s rpath — ``libfoo`` needs its own ``$ORIGIN`` too.
    On macOS the same applies via ``@loader_path``.
    """
    if sys.platform == "darwin":
        rpath = "@loader_path"
        patcher = "install_name_tool"
        arguments = ["-add_rpath", rpath]
    elif sys.platform == "linux":
        rpath = "$ORIGIN"
        patcher = "patchelf"
        arguments = ["--add-rpath", rpath]
    else:
        return

    targets: set[Path] = set()
    for pkg_dir in _package_dirs_with_native_extensions(staging_dir):
        for path in pkg_dir.iterdir():
            if _is_shared_library(path) or _is_python_extension_module(path):
                targets.add(path)

    warned = False
    for path in targets:
        try:
            subprocess.run(
                [patcher, *arguments, str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            if not warned:
                print(
                    f"WARNING: {patcher} not found. Bundled shared libs and Python "
                    f"extensions in the wheel may fail to load their transitive deps. "
                    f"Install {patcher} or run auditwheel repair on the wheel.",
                    flush=True,
                )
                warned = True
        except subprocess.CalledProcessError:
            pass
