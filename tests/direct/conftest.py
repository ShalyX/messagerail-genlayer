"""Windows compatibility shim for genlayer-test direct mode."""

import os
import tempfile


_original_unlink = os.unlink


def _unlink_windows_compatible(path, *args, **kwargs):
    try:
        return _original_unlink(path, *args, **kwargs)
    except PermissionError as error:
        temp_root = os.path.abspath(tempfile.gettempdir())
        candidate = os.path.abspath(os.fspath(path))
        if getattr(error, "winerror", None) == 32 and candidate.startswith(temp_root):
            return None
        raise


os.unlink = _unlink_windows_compatible
