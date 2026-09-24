"""Checksum-verified audio transfer over the existing WireGuard/SSH route."""
import hashlib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config import get_settings


@dataclass(frozen=True)
class AudioExportResult:
    remote_dir: str
    remote_audio_path: str
    remote_checksum_path: str
    sha256: str
    local_deleted: bool


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_path(consultation_id: str, filename: str) -> str:
    settings = get_settings()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", consultation_id):
        raise ValueError("Некорректный ID записи")
    if filename not in {"audio.mp3", "audio.wav", "audio.ogg", "audio.opus", "audio.m4a", "audio.webm", "audio.mp4"}:
        raise ValueError("Некорректное имя аудиофайла")
    return f"{settings.remote_audio_base_dir.rstrip('/')}/{consultation_id}/{filename}"


def _remote_target(remote_path: str) -> str:
    settings = get_settings()
    return f"{settings.remote_audio_user}@{settings.remote_audio_host}:{shlex.quote(remote_path)}"


def _ssh_command() -> list[str]:
    settings = get_settings()
    command = ["ssh", "-p", str(settings.remote_audio_port)]
    if settings.remote_audio_ssh_key.strip():
        command.extend(["-i", settings.remote_audio_ssh_key.strip()])
    command.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"])
    return command


def _run(command: list[str], timeout: int = 1800) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Команда {command[0]} не завершилась: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError((detail or f"Команда завершилась с кодом {result.returncode}")[:1000])
    return result.stdout or ""


def _check_configuration() -> None:
    settings = get_settings()
    if not settings.remote_audio_enabled:
        raise RuntimeError("Выгрузка аудио отключена: REMOTE_AUDIO_ENABLED=false")
    if not settings.remote_audio_host or not settings.remote_audio_user or not settings.remote_audio_base_dir:
        raise RuntimeError("Не заполнены REMOTE_AUDIO_HOST, REMOTE_AUDIO_USER или REMOTE_AUDIO_BASE_DIR")


def export_audio_file(consultation_id: str, audio_path: Path) -> AudioExportResult:
    _check_configuration()
    if not audio_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {audio_path}")
    settings = get_settings()
    remote_audio = _remote_path(consultation_id, audio_path.name)
    remote_dir = str(Path(remote_audio).parent)
    checksum = _sha256_file(audio_path)
    checksum_path = audio_path.with_name(f"{audio_path.name}.sha256")
    checksum_path.write_text(f"{checksum}  {audio_path.name}\n", encoding="utf-8")

    ssh_command = _ssh_command()
    remote_host = f"{settings.remote_audio_user}@{settings.remote_audio_host}"
    mkdir_command = [*ssh_command, remote_host, f"mkdir -p -- {shlex.quote(remote_dir)}"]
    rsync_command = [
        "rsync", "-a", "-e", " ".join(shlex.quote(part) for part in ssh_command),
        str(audio_path), str(checksum_path), _remote_target(remote_dir) + "/",
    ]
    try:
        _run(mkdir_command)
        _run(rsync_command)
        hash_output = _run([*ssh_command, remote_host,
                            f"sha256sum -- {shlex.quote(remote_audio)}"], timeout=120).split()
        if not hash_output:
            raise RuntimeError("Удалённый сервер не вернул контрольную сумму аудио")
        remote_hash = hash_output[0]
        if remote_hash.lower() != checksum:
            raise RuntimeError("Контрольная сумма удалённого аудио не совпала")
    finally:
        checksum_path.unlink(missing_ok=True)

    return AudioExportResult(
        remote_dir=remote_dir,
        remote_audio_path=remote_audio,
        remote_checksum_path=remote_audio + ".sha256",
        sha256=checksum,
        local_deleted=False,
    )


def restore_audio_file(consultation_id: str, audio_path: Path, expected_sha256: str | None) -> Path:
    _check_configuration()
    if not expected_sha256 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Некорректная контрольная сумма удалённого аудио")
    remote_audio = _remote_path(consultation_id, audio_path.name)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = audio_path.with_name(f".restore-{uuid4().hex}-{audio_path.name}")
    ssh_command = _ssh_command()
    try:
        _run(["rsync", "-a", "-e", " ".join(shlex.quote(part) for part in ssh_command),
              _remote_target(remote_audio), str(temporary)])
        if _sha256_file(temporary) != expected_sha256:
            raise RuntimeError("Контрольная сумма загруженного аудио не совпала")
        os.replace(temporary, audio_path)
    finally:
        temporary.unlink(missing_ok=True)
    return audio_path
