"""Locate isolated CUDA wheel libraries before launching Python subprocesses."""
import ctypes
import os
from pathlib import Path
import sysconfig


def library_environment(environment, sites=None):
    result = dict(environment)
    roots = sites if sites is not None else [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
    directories = []
    for root in roots:
        if not root:
            continue
        root = Path(root)
        directories.extend(sorted(root.glob("nvidia/*/lib")))
        directories.append(root / "torch" / "lib")
    directories.extend(Path(value) for value in ("/usr/local/cuda/lib64", "/usr/local/cuda/targets/x86_64-linux/lib"))
    paths = [str(path.resolve()) for path in directories if path.is_dir()]
    paths.extend(value for value in result.get("LD_LIBRARY_PATH", "").split(os.pathsep) if value)
    result["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(paths))
    return result


def verify_libraries(emit=print):
    # This must run in a NEW Python process with the loader environment already
    # configured. Importing torch alone can mask a missing dynamic-loader path.
    libraries = ("libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9",
                 "libcudnn_ops.so.9", "libcudnn_cnn.so.9")
    handles = []
    for name in libraries:
        try:
            handles.append(ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL))
        except OSError as exc:
            raise RuntimeError(
                f"CUDA runtime cannot load {name}: {exc}. "
                "Check the isolated worker's NVIDIA libraries and LD_LIBRARY_PATH; "
                "GPU detection alone does not verify these libraries."
            ) from exc
    emit("[setup] CUDA 12 cuBLAS and cuDNN 9 shared-library loading passed.")
    return handles
