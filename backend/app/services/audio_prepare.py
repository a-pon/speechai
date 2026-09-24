"""Worker-only audio checks and conversion."""
import math
import subprocess
import shutil
import re
from dataclasses import dataclass
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


class InterruptedAudioError(AudioValidationError):
    pass


@dataclass(frozen=True)
class AudioSignalProfile:
    mean_dbfs: float
    peak_dbfs: float
    long_silence_sec: float
    longest_silence_sec: float


MIN_USABLE_PEAK_DBFS = -60.0
SILENCE_FLOOR_DBFS = -55.0
# MediaRecorder stops on a timer, and MP3 encoders add a short trailing frame.
MAX_DURATION_GRACE_SECONDS = 1.0


def probe_audio_signal(path: Path) -> AudioSignalProfile:
    """Measure volume and long quiet intervals in one streaming decode."""
    command = [get_ffmpeg_exe() if get_ffmpeg_exe else "ffmpeg", "-hide_banner", "-nostats",
               "-xerror", "-threads", "1",
               "-i", str(path), "-map", "0:a:0", "-af",
               f"silencedetect=n={SILENCE_FLOOR_DBFS}dB:d=10,volumedetect", "-f", "null", "-"]
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
    intervals = [float(value) for value in re.findall(r"silence_duration:\s*(\d+(?:\.\d+)?)", summary)]
    return AudioSignalProfile(float(mean[-1]), float(peak[-1]),
                              sum(intervals), max(intervals, default=0.0))


def probe_audio_volume(path: Path) -> tuple[float, float]:
    """Keep the diagnostic command's volume-only interface."""
    profile = probe_audio_signal(path)
    return profile.mean_dbfs, profile.peak_dbfs


def classify_audio_signal(profile: AudioSignalProfile, duration_seconds: float) -> str | None:
    if (profile.peak_dbfs <= MIN_USABLE_PEAK_DBFS
            or profile.long_silence_sec >= duration_seconds * 0.995):
        return "silent_audio"
    if (duration_seconds >= 1800
            and profile.longest_silence_sec >= max(900, duration_seconds * 0.5)):
        return "interrupted_audio"
    return None


def check_usable_audio(path: Path, duration_seconds: float | None = None) -> None:
    """Reject empty and interrupted recordings before paying for recognition."""
    profile = probe_audio_signal(path)
    if duration_seconds is None:
        duration_seconds = get_duration_seconds(path)
    if duration_seconds is None or duration_seconds <= 0:
        raise AudioValidationError("Не удалось определить длительность аудиозаписи")
    category = classify_audio_signal(profile, duration_seconds)
    if category == "silent_audio":
        detail = ("Звуковой сигнал слишком тихий: проверьте исходную аудиозапись"
                  if profile.peak_dbfs <= MIN_USABLE_PEAK_DBFS
                  else "Почти вся аудиозапись без звука: проверьте микрофон")
        raise SilentAudioError(detail)
    if category == "interrupted_audio":
        raise InterruptedAudioError("В аудиозаписи длительный участок без звука: проверьте микрофон")


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
    if not getattr(settings, "mock_ai", False):
        check_usable_audio(path, duration_seconds)
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
        except (SilentAudioError, InterruptedAudioError):
            # Keep both files so an administrator can compare source and conversion.
            raise
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
    except (SilentAudioError, InterruptedAudioError):
        # A valid source turning silent here points to the conversion path.
        raise
    except AudioValidationError:
        target.unlink(missing_ok=True)
        raise
    return target, normalized_size, normalized_duration
