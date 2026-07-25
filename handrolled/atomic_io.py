"""Crash-safe JSON writes: sibling temp -> fsync -> os.replace.

The approach is lifted from a production audit CLI where a truncated
checkpoint would have cost hours of paid LLM judging.
A plain Path.write_text is not crash-safe: a kill -9, an OOM or a laptop sleep
during the write leaves a truncated file on disk, and a truncated checkpoint is
worse than no checkpoint because it resumes into nonsense.

os.replace is atomic on POSIX. fsync on the temp file forces the data to disk
before the rename; fsync on the containing directory forces the rename itself
to disk, which is the step people usually skip.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())
