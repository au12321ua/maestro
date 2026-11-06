"""Pack stored file sets without changing their paths, bytes or executable bits."""

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from maestro.core.errors import MaestroError


def write_archive(store, files, folder: str, missing_code: str) -> Path:
    """Build completely before delivery; remove partial output on any failure."""
    store.root.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".archive-", suffix=".zip", dir=store.root)
    os.close(handle)
    target = Path(name)
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in files:
                blob = store.blob(entry["sha256"])
                if blob is None:
                    raise MaestroError(missing_code, "The stored content is gone", 410)
                info = zipfile.ZipInfo(f"{folder}/{entry['path']}")
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100000 | (0o755 if entry["executable"] else 0o644)) << 16
                with blob.open("rb") as source, archive.open(info, "w") as sink:
                    shutil.copyfileobj(source, sink, 1 << 20)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target
