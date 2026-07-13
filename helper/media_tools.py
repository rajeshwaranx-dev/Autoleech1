import asyncio
import os
from pathlib import Path
from typing import Optional
import json

from config import Config

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


async def get_video_duration(input_path: str) -> float:
    """Return video duration in seconds, or 0 when ffprobe cannot read it."""
    code, out, _ = await run_cmd(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", input_path,
    )
    if code != 0:
        return 0.0
    try:
        return float(json.loads(out).get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


async def add_video_branding(input_path: str, output_path: str, watermark_text: str, metadata_text: str) -> str:
    """Add bottom-center watermark text and Telegram-facing metadata to a video.

    The watermark is visible for the whole video when the video is 5 minutes or
    shorter. For longer videos, it is visible only for the first 5% of runtime.
    Falls back to stream-copy metadata only when drawtext is unavailable. Returns
    the path that should be uploaded.
    """
    if not watermark_text:
        watermark_text = metadata_text or "Join @MNTGX in Telegram"
    duration = await get_video_duration(input_path)
    watermark_until = duration if 0 < duration <= 300 else duration * 0.05
    enable_expr = f":enable='between(t,0,{watermark_until:.3f})'" if watermark_until > 0 else ""
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    escaped = watermark_text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    drawtext = (
        f"drawtext=fontfile={font}:text='{escaped}':"
        r"x=(w-text_w)/2:y=h-text_h-(h*0.055):fontsize=max(22\,h/24):"
        "fontcolor=white@0.78:borderw=2:bordercolor=black@0.35"
        f"{enable_expr}"
    )
    cmd = [
        "ffmpeg", "-y", "-threads", str(Config.FFMPEG_THREADS), "-i", input_path, "-vf", drawtext,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-threads", str(Config.FFMPEG_THREADS),
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
        "ffmpeg", "-y", "-threads", str(Config.FFMPEG_THREADS), "-i", input_path, "-map", "0", "-c", "copy",
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
