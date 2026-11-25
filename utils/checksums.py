"""SHA-256 checksums of files, streamed so a large artifact does not have to fit in memory."""
import hashlib
import json
import os


def sha256_of_file(path: str) -> str:
    """The SHA-256 of one file, in hex."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_files(paths: tuple[str, ...]) -> str:
    """One SHA-256 for a document: the checksum of the file for one file, else one over all.

    For several files it hashes the name and the checksum of each file in order, so
    renaming, reordering or editing any file changes it.
    """
    if len(paths) == 1:
        return sha256_of_file(paths[0])
    h = hashlib.sha256()
    for path in paths:
        h.update(f"{os.path.basename(path)}:{sha256_of_file(path)}\n".encode())
    return h.hexdigest()


def sha256_of_json(value: object) -> str:
    """The SHA-256 of a JSON value, written with sorted keys, so one value gives one checksum.

    Raises:
        TypeError, ValueError: the value is not JSON, or holds NaN or an infinity.
    """
    text = json.dumps(value, sort_keys=True, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()
