"""Detect and extract zip archives so a leeched .zip's real contents get uploaded
individually instead of uploading the zip file itself.

Design constraints this respects:
- Zip bombs: a small zip can decompress to an enormous size. zipfile's central
  directory (ZipInfo.file_size) gives each entry's real uncompressed size WITHOUT
  extracting anything, so the total is checked against MAX_TOTAL_UNCOMPRESSED_BYTES
  before a single byte is written to disk -- not after, when the damage is already
  done.
- Entry count: a pathological archive with thousands of tiny files would flood the
  chat with that many uploads. MAX_EXTRACTED_FILES caps how many are extracted;
  anything beyond that is skipped and reported, not silently dropped without saying
  so.
- Directory entries and path traversal: directory-only entries (ZipInfo.is_dir())
  are skipped since they aren't files to upload. Member names are resolved through
  Path and checked to stay inside the destination directory, rejecting the classic
  "../../etc/passwd"-style zip-slip path traversal some malicious/malformed zips use.
"""

import zipfile
from pathlib import Path
from typing import NamedTuple

MAX_TOTAL_UNCOMPRESSED_BYTES = int(20 * 1024 * 1024 * 1024)  # 20GB safety cap against zip bombs
MAX_EXTRACTED_FILES = 200  # cap on how many individual files get uploaded from one zip


class ZipExtractionResult(NamedTuple):
    files: list[Path]
    skipped_count: int  # entries beyond MAX_EXTRACTED_FILES that were not extracted
    total_uncompressed_bytes: int


def is_zip_file(path: Path) -> bool:
    """True if `path` is a genuine zip archive. Checks the actual file signature via
    zipfile.is_zipfile rather than trusting the .zip extension, since a leeched file
    could be misnamed."""
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def _is_safe_member_path(dest_dir: Path, member_name: str) -> Path | None:
    """Resolve a zip member name against dest_dir, rejecting any path that would
    escape it (zip-slip / path traversal). Returns the safe absolute path, or None
    if the member name is unsafe."""
    candidate = (dest_dir / member_name).resolve()
    try:
        candidate.relative_to(dest_dir.resolve())
    except ValueError:
        return None
    return candidate


def extract_zip(zip_path: Path, dest_dir: Path) -> ZipExtractionResult:
    """Extract `zip_path`'s real files into dest_dir, returning the paths of files
    actually written. Raises RuntimeError if the archive's total uncompressed size
    exceeds MAX_TOTAL_UNCOMPRESSED_BYTES, checked entirely from the central
    directory before any extraction happens.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        entries = [info for info in zf.infolist() if not info.is_dir()]
        total_uncompressed = sum(info.file_size for info in entries)

        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                f"This zip would decompress to {total_uncompressed / (1024**3):.1f}GB, "
                f"over the {MAX_TOTAL_UNCOMPRESSED_BYTES / (1024**3):.0f}GB safety limit. "
                "Refusing to extract (this is either a very large archive or a zip bomb)."
            )

        to_extract = entries[:MAX_EXTRACTED_FILES]
        skipped_count = len(entries) - len(to_extract)

        extracted_files: list[Path] = []
        for info in to_extract:
            safe_path = _is_safe_member_path(dest_dir, info.filename)
            if safe_path is None:
                # Zip-slip attempt or otherwise unsafe path -- skip this entry rather
                # than let it write outside dest_dir.
                skipped_count += 1
                continue
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(safe_path, "wb") as out:
                # Stream in chunks rather than zf.extract()'s default, matching the
                # same bounded-memory philosophy as the rest of this codebase's
                # download/split helpers.
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            extracted_files.append(safe_path)

    return ZipExtractionResult(
        files=extracted_files,
        skipped_count=skipped_count,
        total_uncompressed_bytes=total_uncompressed,
    )
