"""Filename validation at trust boundaries."""

import re
import unicodedata
from pathlib import PurePosixPath


class UnsafeFileType(ValueError):
    """Raised when a requested artifact type is not allowlisted."""


def sanitize_filename(
    original: str,
    *,
    allowed_extensions: set[str],
    max_length: int = 100,
) -> str:
    normalized_path = original.replace("\\", "/").replace("\x00", "")
    basename = PurePosixPath(normalized_path).name
    stem, dot, raw_extension = basename.rpartition(".")
    extension = f".{raw_extension.lower()}" if dot else ""
    allowed = {item.lower() for item in allowed_extensions}
    if extension not in allowed:
        raise UnsafeFileType(f"file extension {extension or '<none>'!r} is not allowed")

    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    safe_stem = re.sub(r"[^A-Za-z0-9._ -]+", "", ascii_stem)
    safe_stem = re.sub(r"[\s_-]+", "_", safe_stem).strip("._-")
    if not safe_stem:
        safe_stem = "presentation"

    room_for_stem = max_length - len(extension)
    if room_for_stem < 1:
        raise ValueError("max_length is too small for the extension")
    return f"{safe_stem[:room_for_stem]}{extension}"
