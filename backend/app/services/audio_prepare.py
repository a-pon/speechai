"""Worker-only audio checks and conversion."""
import subprocess
import shutil
import re
from pathlib import Path

from app.config import get_settings
from app.services.audio_utils import get_duration_sec

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:
    get_ffmpeg_exe = None


class AudioValidationError(ValueError):
    pass


class SilentAudioError(AudioValidationError):
    pass


def has_usable_signal(path: Path) -> bool:
    """Check the full decoded recording without keeping audio samples in worker RAM."""
    command = [get_ffmpeg_exe() if get_ffmpeg_exe else "ffmpeg", "-hide_banner", "-nostats",
               "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, timeout=1800, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AudioValidationError(f"Проверка звука: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise AudioValidationError(f"Не удалось проверить звуковой сигнал: {(result.stderr or '')[-500:]}")
    matches = re.findall(r"max_volume:\s*(-inf|-?\d+(?:\.\d+)?)\s*dB", result.stderr or "")
    if not matches:
        raise AudioValidationError("Не удалось определить уровень звукового сигнала")
    # A conservative floor: the confirmed silent recording peaks at -68 dBFS.
    return float(matches[-1]) > -60.0


def validate_audio(path: Path) -> tuple[int, int]:
    settings = get_settings()
    if not path.is_file():
        raise AudioValidationError("Файл аудиозаписи не найден на сервере")
    size = path.stat().st_size
    if size <= 0 or size > settings.max_audio_upload_mb * 1024 * 1024:
        raise AudioValidationError(f"Размер аудиозаписи должен быть от 1 байта до {settings.max_audio_upload_mb} МБ")
    duration = get_duration_sec(path)
    if duration is None or duration <= 0:
        raise AudioValidationError("Не удалось определить длительность аудиозаписи")
    if duration > settings.max_audio_duration_minutes * 60:
        raise AudioValidationError(f"Аудиозапись не должна быть длиннее {settings.max_audio_duration_minutes} минут")
    if not getattr(settings, "mock_ai", False) and not has_usable_signal(path):
        raise SilentAudioError("Звуковой сигнал слишком тихий: проверьте исходную аудиозапись")
    return size, duration


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
