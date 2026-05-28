from __future__ import annotations

import hashlib
import importlib.machinery
import shutil
import subprocess
import sys
from pathlib import Path


def _is_python_extension_module(path: Path) -> bool:
    """True if *path* is a real file whose name matches ``EXTENSION_SUFFIXES``."""
    if path.is_symlink():
        return False
    name = path.name
    for suf in importlib.machinery.EXTENSION_SUFFIXES:
        if not name.endswith(suf):
            continue
        # Bare ".so" is ambiguous: Python extensions and plain shared-lib stubs (libfmt.so) both use it.
        if suf == ".so" and name.startswith("lib"):
            return False
        return True
    return False


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
    """macOS/Linux: add ``@loader_path`` / ``$ORIGIN`` to extension ``.so`` files."""
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

    warned = False
    for path in staging_dir.rglob("*.so"):
        if _is_python_extension_module(path):
            try:
                subprocess.run(
                    [patcher, *arguments, str(path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                print(
                    f"WARNING: {patcher} not found. Python extension {path.name} may not load "
                    f"shared libs. Install {patcher} or run auditwheel repair on the wheel {path.name}.",
                    flush=True,
                )
                warned = True
            except subprocess.CalledProcessError:
                pass


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def _mangle_linux_name(name: str, h: str) -> str:
    """libfmt.so.12.1.0 → libfmt-a1b2c3d4.so.12.1.0"""
    idx = name.find(".so")
    if idx == -1:
        return name
    return f"{name[:idx]}-{h}{name[idx:]}"


def _mangle_darwin_name(name: str, h: str) -> str:
    """libfmt.12.1.0.dylib → libfmt-a1b2c3d4.12.1.0.dylib"""
    if not name.endswith(".dylib"):
        return name
    stem = name[:-6]
    dot = stem.find(".")
    if dot == -1:
        return f"{stem}-{h}.dylib"
    return f"{stem[:dot]}-{h}{stem[dot:]}.dylib"


def mangle_sonames(staging_dir: Path) -> None:
    """Mangle SONAMEs of bundled shared libs to prevent runtime symbol conflicts."""
    if sys.platform == "linux":
        _mangle_linux(staging_dir)
    elif sys.platform == "darwin":
        _mangle_darwin(staging_dir)


def _mangle_linux(staging_dir: Path) -> None:
    for pkg_dir in _package_dirs_with_native_extensions(staging_dir):
        bundled = [
            p for p in pkg_dir.iterdir()
            if p.is_file() and ".so" in p.name and not _is_python_extension_module(p)
        ]
        if not bundled:
            continue

        by_hash: dict[str, list[Path]] = {}
        for lib in bundled:
            by_hash.setdefault(_hash_file(lib), []).append(lib)

        soname_map: dict[str, str] = {}
        for h, files in by_hash.items():
            try:
                soname = subprocess.run(
                    ["patchelf", "--print-soname", str(files[0])],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
            if not soname:
                continue
            soname_map[soname] = _mangle_linux_name(soname, h)
            for f in files:
                new_path = f.parent / _mangle_linux_name(f.name, h)
                f.rename(new_path)
                try:
                    subprocess.run(
                        ["patchelf", "--set-soname", soname_map[soname], str(new_path)],
                        check=True, capture_output=True,
                    )
                except subprocess.CalledProcessError:
                    pass

        if not soname_map:
            continue

        for path in pkg_dir.rglob("*.so*"):
            if not path.is_file():
                continue
            for old, new in soname_map.items():
                try:
                    if old in subprocess.run(
                        ["patchelf", "--print-needed", str(path)],
                        capture_output=True, text=True,
                    ).stdout:
                        subprocess.run(
                            ["patchelf", "--replace-needed", old, new, str(path)],
                            check=True, capture_output=True,
                        )
                except (FileNotFoundError, subprocess.CalledProcessError):
                    pass


def _mangle_darwin(staging_dir: Path) -> None:
    for pkg_dir in _package_dirs_with_native_extensions(staging_dir):
        bundled = [p for p in pkg_dir.iterdir() if p.is_file() and p.name.endswith(".dylib")]
        if not bundled:
            continue

        by_hash: dict[str, list[Path]] = {}
        for lib in bundled:
            by_hash.setdefault(_hash_file(lib), []).append(lib)

        install_name_map: dict[str, str] = {}
        for h, files in by_hash.items():
            try:
                lines = subprocess.run(
                    ["otool", "-D", str(files[0])],
                    capture_output=True, text=True, check=True,
                ).stdout.strip().splitlines()
                install_name = lines[-1].strip() if len(lines) > 1 else ""
            except subprocess.CalledProcessError:
                continue
            if not install_name:
                continue
            basename = install_name.rsplit("/", 1)[-1]
            prefix = install_name[: len(install_name) - len(basename)]
            new_install_name = f"{prefix}{_mangle_darwin_name(basename, h)}"
            install_name_map[install_name] = new_install_name
            for f in files:
                new_path = f.parent / _mangle_darwin_name(f.name, h)
                f.rename(new_path)
                try:
                    subprocess.run(
                        ["install_name_tool", "-id", new_install_name, str(new_path)],
                        check=True, capture_output=True,
                    )
                except subprocess.CalledProcessError:
                    pass

        if not install_name_map:
            continue

        for path in pkg_dir.rglob("*"):
            if not path.is_file() or not (path.name.endswith(".dylib") or path.name.endswith(".so")):
                continue
            for old, new in install_name_map.items():
                try:
                    if old in subprocess.run(
                        ["otool", "-L", str(path)], capture_output=True, text=True,
                    ).stdout:
                        subprocess.run(
                            ["install_name_tool", "-change", old, new, str(path)],
                            check=True, capture_output=True,
                        )
                except subprocess.CalledProcessError:
                    pass
