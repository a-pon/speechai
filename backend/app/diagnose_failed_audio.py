"""Read-only, sequential sound check for recently failed consultations."""

import argparse
import os
from pathlib import Path

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Consultation
from app.services.audio_prepare import AudioValidationError, classify_audio_signal, probe_audio_signal
from app.services.audio_utils import get_duration_seconds


def main() -> None:
    parser = argparse.ArgumentParser(description="Check sound levels in failed recordings without sending audio")
    parser.add_argument("--limit", type=int, default=20, help="number of recent failed records (default: 20)")
    parser.add_argument("--offset", type=int, default=0, help="skip this many newest failed records")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or args.offset < 0:
        parser.error("--limit must be 1..100 and --offset must be 0 or greater")

    # FFmpeg inherits this priority, leaving CPU available for the web app and worker.
    try:
        os.nice(10)
    except OSError:
        pass

    with SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(Consultation).where(Consultation.status == "failed"))
        rows = db.execute(
            select(Consultation.id, Consultation.audio_path, Consultation.duration_sec,
                   Consultation.processing_stage, Consultation.error_category)
            .where(Consultation.status == "failed")
            .order_by(Consultation.created_at.desc(), Consultation.id.desc())
            .offset(args.offset).limit(args.limit)
        ).all()

    print(f"failed_total={total} selected={len(rows)} offset={args.offset}", flush=True)
    candidates = []
    for index, row in enumerate(rows, start=args.offset + 1):
        print(f"checking={index} id={row.id} duration_sec={row.duration_sec or '-'} "
              f"stage={row.processing_stage or '-'} category={row.error_category or '-'}", flush=True)
        path = Path(row.audio_path)
        if not path.is_file():
            print(f"result id={row.id} sound=missing_file", flush=True)
            continue
        try:
            size = path.stat().st_size
            duration = get_duration_seconds(path)
            if duration is None or duration <= 0:
                raise AudioValidationError("Не удалось определить длительность")
            profile = probe_audio_signal(path)
        except (AudioValidationError, OSError):
            print(f"result id={row.id} sound=probe_error", flush=True)
            continue
        category = classify_audio_signal(profile, duration)
        sound = {"silent_audio": "near_silent", "interrupted_audio": "interrupted"}.get(
            category, "has_signal")
        print(f"result id={row.id} sound={sound} bytes={size} "
              f"mean_dbfs={profile.mean_dbfs:.1f} peak_dbfs={profile.peak_dbfs:.1f} "
              f"silence_sec={profile.long_silence_sec:.0f} "
              f"longest_silence_sec={profile.longest_silence_sec:.0f}", flush=True)
        if sound == "has_signal":
            candidates.append(row.id)

    print(f"checked={len(rows)} next_offset={args.offset + len(rows)} "
          f"candidate_ids={','.join(candidates) or 'none'}", flush=True)


if __name__ == "__main__":
    main()
