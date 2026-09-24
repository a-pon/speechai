"""Worker-only audio checks and conversion."""
import subprocess
import shutil
from pathlib import Path

from app.config import get_settings
from app.services.audio_utils import get_duration_sec

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:
    get_ffmpeg_exe = None


class AudioValidationError(ValueError):
    pass


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
