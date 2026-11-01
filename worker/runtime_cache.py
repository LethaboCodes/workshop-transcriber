"""Reuse private Kaggle outputs without mixing in Kaggle's global packages."""
import hashlib
import json
import platform
import shutil
import sys
import sysconfig
import tarfile
from pathlib import Path


def runtime_key(requirements):
    identity = ["runtime-v1", sys.implementation.cache_tag, sysconfig.get_config_var("SOABI"),
                platform.machine(), platform.libc_ver(), Path(requirements).read_text()]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def prepare(project, environment, output, inputs, requirements, emit):
    key = runtime_key(requirements)
    cache = Path(output) / "runtime-cache"
    cache.mkdir(parents=True, exist_ok=True)
    models = cache / "models"
    site = Path(environment) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    reused = False
    for manifest in Path(inputs).rglob("runtime-cache.json"):
        try:
            metadata = json.loads(manifest.read_text())
            source = manifest.parent
            if metadata.get("key") != key or not metadata.get("ready") or not (source / "packages.tar").is_file():
                continue
            emit("[setup] Restoring compatible dependencies from private Kaggle notebook outputs (no package download).")
            with tarfile.open(source / "packages.tar", "r:") as archive:
                archive.extractall(site, filter="data")
            if (source / "models").is_dir():
                emit("[setup] Restoring downloaded model files within Kaggle.")
                shutil.copytree(source / "models", models, symlinks=True,
                                ignore=shutil.ignore_patterns(".locks", "*.incomplete", "token", "stored_tokens"))
            reused = True
            break
        except (OSError, ValueError, tarfile.TarError) as exc:
            emit(f"[setup] Runtime cache unavailable: {exc}; using a clean installation.")
            for path in (site, models):
                # The site directory is the fresh venv's private purelib path.
                if path == site:
                    remove_directory(path, site.parent)
                    site.mkdir()
                    continue
                remove_directory(path, cache)
    models.mkdir(exist_ok=True)
    # Install into a normal venv directory: wheel scripts use paths relative to
    # purelib, which installers cannot reliably resolve through a symlink.
    return {"key": key, "path": cache, "models": models, "site": site,
            "reused": reused, "ready": False}


def remove_directory(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink():
        if path.parent.resolve() != root:
            raise ValueError("Cache cleanup escaped its directory.")
        path.unlink()
    elif path.exists():
        if not path.resolve().is_relative_to(root) or path.resolve() == root:
            raise ValueError("Cache cleanup escaped its directory.")
        shutil.rmtree(path)


def mark_ready(state):
    state["ready"] = True


def physical_size(root):
    return sum(path.stat().st_size for path in Path(root).rglob("*")
               if path.is_file() and not path.is_symlink())


def finish(state, emit, budget=14 * 1024 ** 3):
    """Bound cached models, leaving output space for checkpoints and results."""
    cache = state["path"]
    if not state["ready"]:
        # Never advertise dependencies that failed import/GPU preflight.
        remove_directory(cache, cache.parent)
        return
    emit("[setup] Publishing isolated dependencies to private notebook outputs.")
    # One uncompressed archive avoids CPU-heavy compression and thousands of
    # paginated Kaggle output-list requests. It stays within Kaggle on reuse.
    pending = cache / "packages.partial"
    with tarfile.open(pending, "w:") as archive:
        archive.add(state["site"], arcname=".")
    pending.replace(cache / "packages.tar")
    for name in ("token", "stored_tokens"):
        for credential in state["models"].rglob(name):
            if credential.is_file():
                credential.unlink()
    total = physical_size(cache)
    for model in sorted(state["models"].iterdir(), key=lambda path: path.stat().st_mtime):
        if total <= budget:
            break
        if model.is_dir() and not model.is_symlink():
            size = physical_size(model)
            remove_directory(model, state["models"])
            total -= size
            emit(f"[setup] Omitted {model.name} from the reusable model cache to stay within the output budget.")
    if total > budget:
        emit("[setup] Installed dependencies exceed the runtime-cache budget; not publishing this cache.")
        remove_directory(cache, cache.parent)
    else:
        manifest = cache / "runtime-cache.json"
        temporary = manifest.with_suffix(".partial")
        temporary.write_text(json.dumps({"key": state["key"], "ready": True}))
        temporary.replace(manifest)
        emit(f"[setup] Private runtime cache saved ({total / 1024**3:.1f} GiB) for later notebooks.")
