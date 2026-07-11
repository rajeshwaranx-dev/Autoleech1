import asyncio
import os
import shlex
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


async def run_cmd(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def add_video_branding(input_path: str, output_path: str, watermark_text: str, metadata_text: str) -> str:
    """Add a small centered text watermark and Telegram-facing metadata to a video.

    Falls back to stream-copy metadata only when drawtext is unavailable. Returns the
    path that should be uploaded.
    """
    if not watermark_text:
        watermark_text = "@MNTGX"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    escaped = watermark_text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    drawtext = (
        f"drawtext=fontfile={font}:text='{escaped}':"
        r"x=(w-text_w)/2:y=(h-text_h)/2:fontsize=max(18\,h/32):"
        "fontcolor=white@0.42:borderw=2:bordercolor=black@0.25"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path, "-vf", drawtext,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "copy", "-c:s", "copy",
        "-metadata", f"title={metadata_text}",
        "-metadata", f"comment={metadata_text}",
        "-metadata", f"artist={metadata_text}",
        output_path,
    ]
    code, _, err = await run_cmd(*cmd)
    if code == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path

    print(f"[WARN] Watermark encode failed, trying metadata-only copy: {err[-500:]}")
    copy_path = output_path
    code, _, err = await run_cmd(
        "ffmpeg", "-y", "-i", input_path, "-map", "0", "-c", "copy",
        "-metadata", f"title={metadata_text}",
        "-metadata", f"comment={metadata_text}",
        "-metadata", f"artist={metadata_text}",
        copy_path,
    )
    if code == 0 and os.path.exists(copy_path) and os.path.getsize(copy_path) > 0:
        return copy_path
    print(f"[WARN] Metadata-only ffmpeg failed: {err[-500:]}")
    return input_path


def make_cover_image(output_path: str, title: str, thumb_path: Optional[str] = None, brand: str = "Join @MNTGX") -> Optional[str]:
    """Create a compact cover card with optional thumbnail in the top-right."""
    try:
        img = Image.new("RGB", (1280, 720), (15, 18, 28))
        draw = ImageDraw.Draw(img)
        try:
            title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
            brand_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
        except Exception:
            title_font = brand_font = ImageFont.load_default()
        draw.rectangle((0, 0, 1280, 720), outline=(72, 117, 255), width=12)
        display_title = title[:90]
        draw.text((70, 260), display_title, fill=(245, 247, 255), font=title_font)
        draw.text((70, 335), brand, fill=(105, 210, 255), font=brand_font)
        draw.text((70, 386), "Fast leech • Torrent • Magnet • Telegram upload", fill=(200, 206, 220), font=brand_font)
        if thumb_path and os.path.exists(thumb_path):
            with Image.open(thumb_path) as thumb:
                thumb = thumb.convert("RGB")
                thumb.thumbnail((210, 210))
                img.paste(thumb, (1010, 55))
                draw.rectangle((1004, 49, 1226, 271), outline=(255, 255, 255), width=3)
        img.save(output_path, "JPEG", quality=88)
        return output_path
    except Exception as e:
        print(f"[WARN] Cover image generation failed: {e}")
        return None
