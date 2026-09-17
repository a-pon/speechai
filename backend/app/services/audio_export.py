import hashlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def _remote_target(remote_dir: str) -> str:
    settings = get_settings()
    return f"{settings.remote_audio_user}@{settings.remote_audio_host}:{shlex.quote(remote_dir)}/"


def _ssh_command() -> list[str]:
    settings = get_settings()
    command = ["ssh", "-p", str(settings.remote_audio_port)]
    if settings.remote_audio_ssh_key.strip():
        command.extend(["-i", settings.remote_audio_ssh_key.strip()])
    command.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"])
    return command


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Команда не найдена: {command[0]}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"Команда завершилась с кодом {result.returncode}")


def export_audio_file(consultation_id: str, audio_path: Path) -> AudioExportResult:
    settings = get_settings()
    if not settings.remote_audio_enabled:
        raise RuntimeError("Выгрузка аудио отключена: REMOTE_AUDIO_ENABLED=false")
    if not settings.remote_audio_host or not settings.remote_audio_user or not settings.remote_audio_base_dir:
        raise RuntimeError("Не заполнены REMOTE_AUDIO_HOST, REMOTE_AUDIO_USER или REMOTE_AUDIO_BASE_DIR")
    if not audio_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {audio_path}")

    remote_base = settings.remote_audio_base_dir.rstrip("/")
    remote_dir = f"{remote_base}/{consultation_id}"
    checksum = _sha256_file(audio_path)
    checksum_path = audio_path.with_name(f"{audio_path.name}.sha256")
    checksum_path.write_text(f"{checksum}  {audio_path.name}\n", encoding="utf-8")

    ssh_command = _ssh_command()
    remote_host = f"{settings.remote_audio_user}@{settings.remote_audio_host}"
    mkdir_command = [*ssh_command, remote_host, "mkdir", "-p", remote_dir]
    rsync_command = [
        "rsync",
        "-av",
        "-e",
        " ".join(shlex.quote(part) for part in ssh_command),
        str(audio_path),
        str(checksum_path),
        _remote_target(remote_dir),
    ]

    local_deleted = False
    try:
        _run(mkdir_command)
        _run(rsync_command)
        if settings.remote_audio_delete_local_after_upload:
            audio_path.unlink()
            local_deleted = True
    finally:
        checksum_path.unlink(missing_ok=True)

    return AudioExportResult(
        remote_dir=remote_dir,
        remote_audio_path=f"{remote_dir}/{audio_path.name}",
        remote_checksum_path=f"{remote_dir}/{checksum_path.name}",
        sha256=checksum,
        local_deleted=local_deleted,
    )
