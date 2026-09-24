import math
import re
import subprocess
from pathlib import Path

from mutagen import File as MutagenFile


def get_audio_channels(path: Path) -> int | None:
    """Число каналов (1 = моно, 2 = стерео). None — не удалось определить."""
    try:
        audio = MutagenFile(path)
        if audio is not None and audio.info is not None:
            channels = getattr(audio.info, "channels", None)
            if channels is not None:
                return int(channels)
    except Exception:
        return None
    return None


def get_duration_seconds(path: Path) -> float | None:
    try:
        audio = MutagenFile(path)
        if audio is not None and audio.info is not None and audio.info.length > 0:
            return float(audio.info.length)
    except Exception:
        pass
    # Mutagen does not read every browser WebM container; ffmpeg probes its header.
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg = get_ffmpeg_exe()
    except ImportError:
        ffmpeg = "ffmpeg"
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    # Browser WebM can have no duration header. Decode in the worker to measure it.
    if path.suffix.lower() not in {".webm", ".ogg"}:
        return None
    try:
        scan = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0",
                               "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=3600, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if scan.returncode != 0:
        return None
    times = re.findall(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)", scan.stdout or "")
    if not times:
        return None
    hours, minutes, seconds = times[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def get_duration_sec(path: Path) -> int | None:
    """Whole seconds for display and storage; use the precise value for limits."""
    duration = get_duration_seconds(path)
    return max(1, math.ceil(duration)) if duration is not None else None
