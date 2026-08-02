"""Split a file that's too large to upload into numbered parts, each safely under
the effective Telegram upload limit.

Design constraints this respects:
- Heroku dynos have limited, not-precisely-documented ephemeral disk. Pre-splitting
  an entire large file into N parts before uploading any of them could mean the
  original file plus every part sit on disk simultaneously -- for a 6GB file split
  into 2 parts that's already ~12GB, easily exceeding what a small dyno actually has.
  split_file_streaming() instead writes one part, hands it back to the caller (who
  uploads and deletes it), and only then writes the next part -- bounding extra disk
  usage to roughly one part's size, not the whole split set.
- Telegram's real ceiling (2GB non-Premium, 4GB Premium -- see
  Config.effective_max_upload_size) is treated as a hard boundary that must never be
  reached exactly, only approached with a safety margin, since the exact byte count
  Telegram enforces internally isn't guaranteed to match a naive size comparison to
  the byte. Parts are cut at (limit - PART_SIZE_MARGIN_BYTES), not at the raw limit.
"""

import re
from pathlib import Path
from typing import AsyncIterator

# Cut parts this far under the effective limit, not flush against it. Matches the
# "3.99GB, not 4GB" margin requested for a 4GB ceiling, scaled proportionally so a
# 2GB (non-Premium) ceiling gets a smaller, still-meaningful margin.
PART_SIZE_MARGIN_BYTES = 10 * 1024 * 1024  # 10 MiB

READ_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB per read/write, regardless of part size


def part_size_for_limit(limit_bytes: int) -> int:
    """The actual byte size to cut each part at, given the effective upload limit."""
    return max(1, limit_bytes - PART_SIZE_MARGIN_BYTES)


def part_filename(original_name: str, part_number: int, total_parts: int) -> str:
    """e.g. 'Movie.mkv' part 1 of 3 -> 'Movie.mkv.001'. The .NNN suffix (zero-padded
    to at least 3 digits, or more if there are 1000+ parts) is a long-standing,
    widely-recognized convention for split-archive parts that most reassembly tools
    (including plain `cat part.* > whole`) already handle without any special casing.
    """
    width = max(3, len(str(total_parts)))
    return f"{original_name}.{part_number:0{width}d}"


def is_split_part_name(name: str) -> bool:
    """True if `name` looks like a part produced by part_filename (used by the
    unsplit/rejoin path to recognize which uploaded documents belong together)."""
    return bool(re.search(r"\.\d{3,}$", name))


async def split_file_streaming(source: Path, limit_bytes: int) -> AsyncIterator[tuple[Path, int, int]]:
    """Split `source` into numbered parts under `part_size_for_limit(limit_bytes)`,
    yielding (part_path, part_number, total_parts) one at a time as each part
    finishes writing. The caller is expected to consume (upload) and delete each
    part before the generator produces the next one -- this function itself never
    holds more than one in-progress part on disk at a time.

    total_parts is computed up front from the source's total size, so it's accurate
    from the very first yield (needed for correct zero-padding and progress display),
    even though parts are still written and yielded one at a time.
    """
    part_size = part_size_for_limit(limit_bytes)
    total_size = source.stat().st_size
    total_parts = max(1, -(-total_size // part_size))  # ceil division

    with open(source, "rb") as src:
        for part_number in range(1, total_parts + 1):
            part_path = source.with_name(part_filename(source.name, part_number, total_parts))
            remaining = part_size
            with open(part_path, "wb") as out:
                while remaining > 0:
                    chunk = src.read(min(READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            if part_path.stat().st_size == 0:
                # Only possible if total_size was an exact multiple of part_size and
                # we've already emitted every real byte -- clean up the empty
                # trailing part rather than yield something with nothing in it.
                part_path.unlink(missing_ok=True)
                break
            yield part_path, part_number, total_parts
            
