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


def get_duration_sec(path: Path) -> int | None:
    try:
        audio = MutagenFile(path)
        if audio is not None and audio.info is not None:
            return int(audio.info.length)
    except Exception:
        return None
    return None
