"""Worker-only audio checks and conversion."""
import math
import subprocess
import shutil
import re
from pathlib import Path

from app.config import get_settings
from app.services.audio_utils import get_duration_seconds

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:
    get_ffmpeg_exe = None


class AudioValidationError(ValueError):
    pass


class SilentAudioError(AudioValidationError):
    pass


MIN_USABLE_PEAK_DBFS = -60.0
# MediaRecorder stops on a timer, and MP3 encoders add a short trailing frame.
MAX_DURATION_GRACE_SECONDS = 1.0


def probe_audio_volume(path: Path) -> tuple[float, float]:
    """Return mean and peak dBFS for the full decoded recording."""
    command = [get_ffmpeg_exe() if get_ffmpeg_exe else "ffmpeg", "-hide_banner", "-nostats",
               "-xerror", "-threads", "1",
               "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, timeout=1800, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AudioValidationError(f"Проверка звука: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise AudioValidationError(f"Не удалось проверить звуковой сигнал: {(result.stderr or '')[-500:]}")
    summary = result.stderr or ""
    mean = re.findall(r"mean_volume:\s*(-inf|-?\d+(?:\.\d+)?)\s*dB", summary)
    peak = re.findall(r"max_volume:\s*(-inf|-?\d+(?:\.\d+)?)\s*dB", summary)
    if not mean or not peak:
        raise AudioValidationError("Не удалось определить уровень звукового сигнала")
    return float(mean[-1]), float(peak[-1])


def has_usable_signal(path: Path) -> bool:
    """Check the full decoded recording without keeping audio samples in worker RAM."""
    # A conservative floor: the confirmed silent recording peaks at -68 dBFS.
    return probe_audio_volume(path)[1] > MIN_USABLE_PEAK_DBFS


def validate_audio(path: Path) -> tuple[int, int]:
    settings = get_settings()
    if not path.is_file():
        raise AudioValidationError("Файл аудиозаписи не найден на сервере")
    size = path.stat().st_size
    if size <= 0 or size > settings.max_audio_upload_mb * 1024 * 1024:
        raise AudioValidationError(f"Размер аудиозаписи должен быть от 1 байта до {settings.max_audio_upload_mb} МБ")
    duration_seconds = get_duration_seconds(path)
    if duration_seconds is None or duration_seconds <= 0:
        raise AudioValidationError("Не удалось определить длительность аудиозаписи")
    if duration_seconds > settings.max_audio_duration_minutes * 60 + MAX_DURATION_GRACE_SECONDS:
        raise AudioValidationError(f"Аудиозапись не должна быть длиннее {settings.max_audio_duration_minutes} минут")
    if not getattr(settings, "mock_ai", False) and not has_usable_signal(path):
        raise SilentAudioError("Звуковой сигнал слишком тихий: проверьте исходную аудиозапись")
    return size, max(1, math.ceil(duration_seconds))


def prepare_audio(path: Path) -> tuple[Path, int, int]:
    size, duration = validate_audio(path)
    if path.suffix.lower() not in {".webm", ".ogg", ".m4a", ".mp4"}:
        return path, size, duration
    target = path.with_suffix(".mp3")
    if target.is_file():
        try:
            normalized_size, normalized_duration = validate_audio(target)
            return target, normalized_size, normalized_duration
        except AudioValidationError:
            target.unlink(missing_ok=True)
    if shutil.disk_usage(path.parent).free < 512 * 1024 * 1024:
        raise AudioValidationError("На сервере недостаточно места для перекодирования аудио")
    command = [get_ffmpeg_exe() if get_ffmpeg_exe else "ffmpeg", "-y", "-i", str(path),
               "-vn", "-acodec", "libmp3lame", "-b:a", "128k", str(target)]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, timeout=3600, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        target.unlink(missing_ok=True)
        raise AudioValidationError(f"Перекодирование: {type(exc).__name__}") from exc
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise AudioValidationError(f"Не удалось перекодировать аудио: {(result.stderr or '')[-500:]}")
    try:
        normalized_size, normalized_duration = validate_audio(target)
        if abs(normalized_duration - duration) > 2:
            raise AudioValidationError("Длительность аудио изменилась при перекодировании")
    except AudioValidationError:
        target.unlink(missing_ok=True)
        raise
    return target, normalized_size, normalized_duration
