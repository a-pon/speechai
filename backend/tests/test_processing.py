import asyncio
import hashlib
import json
import math
import struct
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.db as db_module
from app.db import Base
from app.models import Consultation
from app.models import TranscriptSegment
from app.services.pipeline import process_step, record_failure, classify_error
from app.services.processing_state import reset_for_manual_retry
from app.services.speechkit import (
    _parse_recognition_events,
    _request_with_retry,
    start_recognition,
    _iter_recognition_events,
)


class SpeechKitParsingTests(unittest.TestCase):
    def test_start_uses_object_storage_uri(self):
        class Response:
            status_code = 200
            is_error = False
            text = ""

            def json(self):
                return {"id": "operation-1"}

        class Client:
            def __init__(self):
                self.request_json = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                self.request_json = kwargs["json"]
                return Response()

        client = Client()
        settings = SimpleNamespace(yandex_api_key="test", yandex_folder_id="folder")
        with patch("app.services.speechkit.get_settings", return_value=settings), \
             patch("app.services.speechkit.get_audio_channels", return_value=1), \
             patch("app.services.speechkit.httpx.AsyncClient", return_value=client):
            operation_id = asyncio.run(start_recognition(
                "https://storage.yandexcloud.net/private/audio.mp3", Path("audio.mp3")))
        self.assertEqual(operation_id, "operation-1")
        self.assertEqual(client.request_json["uri"], "https://storage.yandexcloud.net/private/audio.mp3")
        self.assertNotIn("content", client.request_json)

    def test_final_uses_words_when_text_is_empty(self):
        events = [{
            "channelTag": "1",
            "audioCursors": {"finalIndex": "3"},
            "final": {"alternatives": [{
                "text": "",
                "words": [
                    {"text": "Добрый", "startTimeMs": "100", "endTimeMs": "400"},
                    {"text": "день", "startTimeMs": "410", "endTimeMs": "700"},
                ],
            }]},
        }]
        segments = _parse_recognition_events(events)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "Добрый день")
        self.assertEqual(segments[0].speaker_role, "patient")
        self.assertEqual((segments[0].start_ms, segments[0].end_ms), (100, 700))

    def test_refinement_replaces_final_and_skips_empty_alternative(self):
        events = [
            {"audioCursors": {"finalIndex": "1"}, "final": {
                "alternatives": [{"text": "", "words": []}, {"text": "Исходный текст"}]
            }},
            {"finalRefinement": {"finalIndex": "1", "normalizedText": {
                "alternatives": [{"text": "Нормализованный текст"}]
            }}},
        ]
        segments = _parse_recognition_events(events)
        self.assertEqual([segment.text for segment in segments], ["Нормализованный текст"])

    def test_streaming_recognition_parser_handles_split_json(self):
        from app.services.speechkit import RecognitionCollector

        payload = b'{"final":{"alternatives":[{"text":"\xd0\xa2\xd0\xb5\xd1\x81\xd1\x82"}]}}'
        class Response:
            is_error = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def aiter_bytes(self, _size):
                for chunk in (payload[:20], payload[20:34], payload[34:], b"\n"):
                    yield chunk

        class Client:
            def stream(self, *_args, **_kwargs):
                return Response()

        settings = SimpleNamespace(yandex_api_key="test", yandex_folder_id="folder")
        async def collect():
            collector = RecognitionCollector()
            async for event in _iter_recognition_events(Client(), "op"):
                collector.add(event)
            return collector.segments()

        with patch("app.services.speechkit.get_settings", return_value=settings):
            segments = asyncio.run(collect())
        self.assertEqual(segments[0].text, "Тест")


class DurablePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temporary_dir.name}/test.db")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.audio = Path(self.temporary_dir.name) / "audio.mp3"
        self.audio.write_bytes(b"audio")
        with self.Session() as db:
            db.add(Consultation(
                id="record-1", consultation_date=date(2026, 9, 24),
                consultation_type="repeat_adult", clinic_division="Петра",
                doctor_name="Врач", patient_name="Пациент", audio_path=str(self.audio),
                original_filename="audio.mp3", status="processing", processing_stage="prepare",
                processing_generation=0, processing_attempts=0,
            ))
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temporary_dir.cleanup()

    def row(self):
        with self.Session() as db:
            return db.get(Consultation, "record-1")

    def test_mock_pipeline_with_real_wave_file(self):
        import wave
        audio_path = self.audio.with_suffix(".wav")
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"".join(
                struct.pack("<h", int(13 * math.sin(2 * math.pi * 440 * i / 8000)))
                for i in range(8000)
            ))
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.audio_path = str(audio_path)
            db.commit()
        settings = SimpleNamespace(mock_ai=True, max_audio_upload_mb=300,
                                   max_audio_duration_minutes=120)
        with patch("app.services.pipeline.SessionLocal", self.Session), \
             patch("app.services.pipeline.get_settings", return_value=settings), \
             patch("app.services.audio_prepare.get_settings", return_value=settings), \
             patch("app.services.yandex_gpt.get_settings", return_value=settings):
            self.assertEqual(asyncio.run(process_step("record-1", 0)), 0)
            self.assertEqual(asyncio.run(process_step("record-1", 0)), 0)
            self.assertIsNone(asyncio.run(process_step("record-1", 0)))
        row = self.row()
        self.assertEqual(row.status, "ready")
        self.assertEqual(row.duration_sec, 1)
        self.assertEqual(row.overall_score, 4.5)

    def test_storage_submission_and_resume_poll_without_resubmission(self):
        settings = SimpleNamespace(mock_ai=False, speechkit_poll_seconds=60, speechkit_max_hours=24)
        with patch("app.services.pipeline.SessionLocal", self.Session), \
             patch("app.services.pipeline.get_settings", return_value=settings), \
             patch("app.services.pipeline.prepare_audio", return_value=(self.audio, 5, 42)), \
             patch("app.services.pipeline.upload_audio", return_value="https://storage.example/audio"), \
             patch("app.services.pipeline.start_recognition", new_callable=AsyncMock, return_value="op-123") as start, \
             patch("app.services.pipeline.poll_recognition", new_callable=AsyncMock,
                   return_value=(True, [TranscriptSegment(speaker_role="doctor", start_ms=0,
                                                          end_ms=1000, text="Добрый день", order_index=0)])):
            self.assertEqual(asyncio.run(process_step("record-1", 0)), 0)
            self.assertEqual(self.row().processing_stage, "storage")
            self.assertEqual(asyncio.run(process_step("record-1", 0)), 0)
            self.assertEqual(self.row().processing_stage, "submit")
            self.assertEqual(asyncio.run(process_step("record-1", 0)), 60)
            self.assertEqual(self.row().speechkit_operation_id, "op-123")
            self.assertEqual(self.row().processing_stage, "poll")
            # Simulate a worker restart: the durable ID survives in a new session.
            self.assertEqual(asyncio.run(process_step("record-1", 0)), 0)
            self.assertEqual(self.row().processing_stage, "evaluate")
            self.assertIn("Добрый день", self.row().transcript_text)
            self.assertEqual(start.await_count, 1)

    def test_submitting_without_id_does_not_resubmit(self):
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.processing_stage = "submitting"
            db.commit()
        with patch("app.services.pipeline.SessionLocal", self.Session), \
             patch("app.services.pipeline.start_recognition", new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(RuntimeError, "ID операции не сохранён"):
                asyncio.run(process_step("record-1", 0))
            delay = record_failure("record-1", 0, RuntimeError("uncertain"))
        self.assertIsNone(delay)
        self.assertEqual(self.row().error_category, "submission_unknown")
        self.assertEqual(self.row().status, "failed")
        start.assert_not_awaited()

    def test_long_evaluation_checkpoints_each_chunk(self):
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.processing_stage = "evaluate"
            row.transcript_text = "[Врач] " + "слово " * 3600
            db.commit()
        with patch("app.services.pipeline.SessionLocal", self.Session), \
             patch("app.services.pipeline.summarize_transcript_chunk", new_callable=AsyncMock,
                   return_value="факт и цитата") as summarize, \
             patch("app.services.pipeline.evaluate_from_summaries", new_callable=AsyncMock,
                   return_value=("Общий балл: 4 из 5", 4.0)) as final:
            for _ in range(3):
                self.assertEqual(asyncio.run(process_step("record-1", 0)), 0)
            self.assertEqual(self.row().status, "processing")
            self.assertEqual(len(json.loads(self.row().evaluation_chunks_json)), 3)
            self.assertIsNone(asyncio.run(process_step("record-1", 0)))
        self.assertEqual(summarize.await_count, 3)
        final.assert_awaited_once()
        self.assertEqual(self.row().status, "ready")

    def test_definite_rate_limit_can_retry_submission(self):
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.processing_stage = "submitting"
            db.commit()
        with patch("app.services.pipeline.SessionLocal", self.Session):
            delay = record_failure("record-1", 0, RuntimeError("SpeechKit HTTP 429: rate limit"))
        self.assertEqual(delay, 60)
        self.assertEqual(self.row().processing_stage, "submit")
        self.assertEqual(self.row().status, "processing")
        self.assertEqual(classify_error(RuntimeError("SpeechKit HTTP 503"), "submitting"),
                         ("submission_unknown", False))

    def test_recovery_marks_due_row_and_claims_once(self):
        from app.tasks import _claim, recover_pending_task
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.status = "uploaded"
            db.commit()
        with patch("app.tasks.SessionLocal", self.Session), \
             patch("app.tasks._schedule_next") as schedule:
            recover_pending_task.run()
            recover_pending_task.run()
            schedule.assert_called_once_with("record-1", 0, 0)
            self.assertTrue(_claim("record-1", 0))
            self.assertFalse(_claim("record-1", 0))
            self.assertEqual(self.row().status, "processing")

    def test_upload_and_duration_limits_use_actual_file(self):
        from app.services.audio_prepare import AudioValidationError, validate_audio
        settings = SimpleNamespace(max_audio_upload_mb=1, max_audio_duration_minutes=120)
        with patch("app.services.audio_prepare.get_settings", return_value=settings), \
             patch("app.services.audio_prepare.get_duration_sec", return_value=7201):
            with self.assertRaises(AudioValidationError):
                validate_audio(self.audio)
        self.audio.write_bytes(b"x" * (1024 * 1024 + 1))
        with patch("app.services.audio_prepare.get_settings", return_value=settings):
            with self.assertRaises(AudioValidationError):
                validate_audio(self.audio)

    def test_silence_check_decodes_full_audio_and_accepts_speech_level_signal(self):
        import wave
        from app.services.audio_prepare import SilentAudioError, validate_audio

        audio_path = self.audio.with_suffix(".wav")
        settings = SimpleNamespace(mock_ai=False, max_audio_upload_mb=1,
                                   max_audio_duration_minutes=120)
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 8000)
        with patch("app.services.audio_prepare.get_settings", return_value=settings):
            with self.assertRaises(SilentAudioError):
                validate_audio(audio_path)

        samples = (int(8000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000))
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"".join(struct.pack("<h", value) for value in samples))
        with patch("app.services.audio_prepare.get_settings", return_value=settings):
            self.assertEqual(validate_audio(audio_path)[1], 1)

    def test_empty_result_from_silent_record_is_not_retried(self):
        from app.services.audio_prepare import SilentAudioError

        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.processing_stage = "poll"
            row.speechkit_operation_id = "op-123"
            row.speechkit_started_at = datetime.utcnow()
            row.recognition_empty_polls = 12
            db.commit()
        settings = SimpleNamespace(speechkit_max_hours=24, speechkit_poll_seconds=60,
                                   auto_retry_max_hours=24)
        with patch("app.services.pipeline.SessionLocal", self.Session), \
             patch("app.services.pipeline.get_settings", return_value=settings), \
             patch("app.services.pipeline.poll_recognition", new_callable=AsyncMock,
                   return_value=(True, None)), \
             patch("app.services.pipeline.has_usable_signal", return_value=False), \
             patch("app.services.pipeline.start_recognition", new_callable=AsyncMock) as start:
            with self.assertRaises(SilentAudioError) as failure:
                asyncio.run(process_step("record-1", 0))
            self.assertIsNone(record_failure("record-1", 0, failure.exception))
        self.assertEqual(self.row().status, "invalid_audio")
        self.assertEqual(self.row().error_category, "silent_audio")
        start.assert_not_awaited()

    def test_manual_retry_invalidates_old_generation(self):
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            generation = reset_for_manual_retry(row)
            db.commit()
        self.assertEqual(generation, 1)
        with patch("app.services.pipeline.SessionLocal", self.Session):
            self.assertIsNone(asyncio.run(process_step("record-1", 0)))

    def test_transient_failure_expires_after_retry_window(self):
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.processing_stage = "poll"
            row.stage_first_failure_at = datetime.utcnow() - timedelta(hours=25)
            db.commit()
        with patch("app.services.pipeline.SessionLocal", self.Session):
            self.assertIsNone(record_failure("record-1", 0, RuntimeError("HTTP 503")))
        self.assertEqual(self.row().status, "failed")
        self.assertEqual(self.row().error_category, "speechkit_transient")
        self.assertEqual(classify_error(RuntimeError("Object Storage HTTP 503"), "storage"),
                         ("object_storage", True))
        self.assertEqual(classify_error(RuntimeError("Object Storage HTTP 403"), "storage"),
                         ("object_storage", False))
        self.assertEqual(classify_error(RuntimeError("YandexGPT HTTP 503"), "evaluate"),
                         ("yandexgpt_transient", True))

    def test_three_worker_interruptions_stop_oom_loop(self):
        from app.tasks import recover_pending_task
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.step_running = True
            row.worker_interruptions = 2
            row.lease_until = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        with patch("app.tasks.SessionLocal", self.Session), patch("app.tasks._schedule_next") as schedule:
            recover_pending_task.run()
        schedule.assert_not_called()
        self.assertEqual(self.row().status, "failed")
        self.assertEqual(self.row().error_category, "worker_interrupted")

    def test_bulk_retry_selects_only_failed_and_uploaded(self):
        from app.api.consultations import retry_all_failed_consultations
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.status = "failed"
            for record_id, status in (("ready-1", "ready"), ("uploaded-1", "uploaded"),
                                      ("uncertain-1", "failed"), ("silent-1", "invalid_audio")):
                db.add(Consultation(
                    id=record_id, consultation_date=date(2026, 9, 24), doctor_name="Врач",
                    patient_name="Пациент", audio_path=str(self.audio), original_filename="audio.mp3",
                    status=status, processing_stage="submitting" if record_id == "uncertain-1" else "prepare",
                ))
            db.commit()
            with patch("app.api.consultations._enqueue_processing", return_value=True) as enqueue:
                result = retry_all_failed_consultations(db=db, user={"role": "admin"})
            self.assertEqual((result.selected, result.queued, result.skipped_uncertain), (2, 2, 1))
            self.assertEqual(enqueue.call_count, 2)
            self.assertEqual(db.get(Consultation, "ready-1").status, "ready")
            self.assertEqual(db.get(Consultation, "silent-1").processing_generation, 0)
            self.assertEqual(db.get(Consultation, "uncertain-1").processing_generation, 0)
            with self.assertRaises(HTTPException) as denied:
                retry_all_failed_consultations(db=db, user={"role": "doctor"})
            self.assertEqual(denied.exception.status_code, 403)

    def test_remote_service_controls_are_admin_only(self):
        from app.api.consultations import export_consultation_audio, get_consultation
        with self.Session() as db:
            row = db.get(Consultation, "record-1")
            row.status = "ready"
            row.remote_export_status = "failed"
            row.remote_export_error = "SSH unavailable"
            db.commit()
            settings = SimpleNamespace(remote_audio_enabled=True, remote_audio_auto_export=True)
            with patch("app.api.consultations.get_settings", return_value=settings):
                doctor_detail = get_consultation("record-1", db=db,
                    user={"role": "doctor", "doctor_name": "Врач", "can_view_all_records": False})
                self.assertFalse(doctor_detail.export_available)
                self.assertIsNone(doctor_detail.remote_export_error)
                with self.assertRaises(HTTPException) as denied:
                    export_consultation_audio("record-1", db=db, user={"role": "doctor"})
                self.assertEqual(denied.exception.status_code, 403)
                admin_detail = get_consultation("record-1", db=db,
                    user={"role": "admin", "doctor_name": None, "can_view_all_records": True})
                self.assertTrue(admin_detail.export_available)
                self.assertEqual(admin_detail.remote_export_error, "SSH unavailable")


class AccessAndStorageTests(unittest.TestCase):
    def test_error_detail_admin_only(self):
        from app.api.consultations import _admin_error_message
        row = SimpleNamespace(error_message="poll [speechkit]: secret detail", status="failed",
                              processing_attempts=1)
        self.assertIsNone(_admin_error_message(row, "doctor"))
        self.assertEqual(_admin_error_message(row, "admin"), row.error_message)

    def test_private_object_upload_checks_remote_size(self):
        from app.services.object_storage import upload_audio
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.mp3"
            path.write_bytes(b"12345")
            client = SimpleNamespace(upload_file=unittest.mock.Mock(),
                                     head_object=unittest.mock.Mock(return_value={"ContentLength": 5}))
            settings = SimpleNamespace(object_storage_bucket="private",
                                       object_storage_endpoint="https://storage.yandexcloud.net")
            with patch("app.services.object_storage._client", return_value=client), \
                 patch("app.services.object_storage.get_settings", return_value=settings):
                uri = upload_audio(path, "consultations/id/audio.mp3")
            self.assertEqual(uri, "https://storage.yandexcloud.net/private/consultations/id/audio.mp3")
            self.assertEqual(client.upload_file.call_args.args,
                             (str(path), "private", "consultations/id/audio.mp3"))
            client.head_object.assert_called_once_with(Bucket="private", Key="consultations/id/audio.mp3")

    def test_remote_export_keeps_local_file_until_state_is_saved(self):
        from app.services.audio_export import export_audio_file
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.mp3"
            path.write_bytes(b"audio")
            settings = SimpleNamespace(remote_audio_enabled=True, remote_audio_host="10.0.0.2",
                                       remote_audio_user="speechai", remote_audio_base_dir="/srv/audio",
                                       remote_audio_port=22, remote_audio_ssh_key="",
                                       remote_audio_delete_local_after_upload=True)
            checksum = hashlib.sha256(b"audio").hexdigest()
            with patch("app.services.audio_export.get_settings", return_value=settings), \
                 patch("app.services.audio_export._run", side_effect=["", "", f"{checksum}  file"]):
                result = export_audio_file("7ff0f789-81a8-4eaa-9d20-438812a907ea", path)
            self.assertTrue(path.exists())
            self.assertEqual(result.sha256, checksum)
            self.assertFalse(result.local_deleted)

    def test_remote_restore_rejects_wrong_checksum_without_replacing_local_file(self):
        from app.services.audio_export import restore_audio_file
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.mp3"
            path.write_bytes(b"original")
            settings = SimpleNamespace(remote_audio_enabled=True, remote_audio_host="10.0.0.2",
                                       remote_audio_user="speechai", remote_audio_base_dir="/srv/audio",
                                       remote_audio_port=22, remote_audio_ssh_key="")
            def fake_run(command, timeout=1800):
                Path(command[-1]).write_bytes(b"wrong")
                return ""
            with patch("app.services.audio_export.get_settings", return_value=settings), \
                 patch("app.services.audio_export._run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "Контрольная сумма"):
                    restore_audio_file("7ff0f789-81a8-4eaa-9d20-438812a907ea", path, "a" * 64)
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".restore-*")), [])


class MigrationTests(unittest.TestCase):
    def test_existing_sqlite_database_gets_processing_generation(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE consultations (id VARCHAR(36) PRIMARY KEY, status VARCHAR(32))"))
            connection.execute(text("INSERT INTO consultations (id, status) VALUES ('old-record', 'failed')"))
        settings = SimpleNamespace(database_url="sqlite:///:memory:")
        with patch.object(db_module, "engine", engine), patch.object(db_module, "get_settings", return_value=settings):
            db_module._migrate_sqlite()

        columns = {column["name"] for column in inspect(engine).get_columns("consultations")}
        self.assertIn("processing_generation", columns)
        self.assertTrue({"processing_stage", "lease_until", "queued_at", "storage_key",
                         "speechkit_operation_id", "evaluation_chunks_json", "error_category"} <= columns)
        with engine.connect() as connection:
            generation = connection.scalar(text("SELECT processing_generation FROM consultations WHERE id = 'old-record'"))
        self.assertEqual(generation, 0)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
