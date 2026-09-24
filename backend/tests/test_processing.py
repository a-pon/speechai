import asyncio
import base64
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.db as db_module
from app.db import Base
from app.models import Consultation
from app.models import TranscriptSegment
from app.services.pipeline import process_consultation
from app.services.processing_state import (
    claim_processing,
    recover_pending,
    reset_for_manual_retry,
)
from app.services.speechkit import (
    _audio_request_parts,
    _iter_audio_request,
    _parse_recognition_events,
    _request_with_retry,
    transcribe_audio,
)


class SpeechKitParsingTests(unittest.TestCase):
    def test_streamed_request_has_valid_json_and_exact_length(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            audio_path = Path(temporary_dir) / "audio.mp3"
            raw_audio = bytes(range(256)) * 2300
            audio_path.write_bytes(raw_audio)
            prefix, suffix, length = _audio_request_parts(audio_path, {"recognitionModel": {"model": "general"}})

            async def collect():
                return b"".join([chunk async for chunk in _iter_audio_request(audio_path, prefix, suffix)])

            body = asyncio.run(collect())

        self.assertEqual(len(body), length)
        payload = json.loads(body)
        self.assertEqual(base64.b64decode(payload["content"]), raw_audio)
        self.assertEqual(payload["recognitionModel"]["model"], "general")

    def test_streamed_request_is_recreated_for_retry(self):
        class Response:
            is_error = False
            text = ""

            def __init__(self, status_code):
                self.status_code = status_code

        class Client:
            def __init__(self):
                self.bodies = []

            async def request(self, _method, _url, **kwargs):
                body = b"".join([chunk async for chunk in kwargs["content"]])
                self.bodies.append(body)
                return Response(503 if len(self.bodies) == 1 else 200)

        with tempfile.TemporaryDirectory() as temporary_dir:
            audio_path = Path(temporary_dir) / "audio.mp3"
            audio_path.write_bytes(b"audio data")
            prefix, suffix, length = _audio_request_parts(audio_path, {"recognitionModel": {}})
            client = Client()
            with patch("app.services.speechkit.asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(_request_with_retry(
                    client, "POST", "https://example.test", headers={"Content-Length": str(length)},
                    content_factory=lambda: _iter_audio_request(audio_path, prefix, suffix),
                ))

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(client.bodies), 2)
        self.assertEqual(client.bodies[0], client.bodies[1])
        self.assertEqual(len(client.bodies[0]), length)

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

    def test_completed_operation_waits_for_recognition_result(self):
        class Response:
            status_code = 200
            is_error = False

            def __init__(self, text, payload=None):
                self.text = text
                self.payload = payload

            def json(self):
                return self.payload

        class Client:
            def __init__(self):
                self.result_reads = 0
                self.starts = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, **_kwargs):
                if method == "POST":
                    self.starts += 1
                    return Response("", {"id": "operation-1"})
                if url.endswith("/operation-1"):
                    return Response("", {"done": True})
                self.result_reads += 1
                if self.result_reads == 1:
                    return Response("")
                return Response('{"final":{"alternatives":[{"words":[{"text":"Текст"}]}]}}')

        client = Client()
        settings = SimpleNamespace(mock_ai=False, yandex_api_key="test", yandex_folder_id="folder")
        with tempfile.TemporaryDirectory() as temporary_dir:
            audio_path = Path(temporary_dir) / "audio.mp3"
            audio_path.write_bytes(b"fake audio")
            with patch("app.services.speechkit.get_settings", return_value=settings), \
                 patch("app.services.speechkit.get_audio_channels", return_value=1), \
                 patch("app.services.speechkit.httpx.AsyncClient", return_value=client), \
                 patch("app.services.speechkit.asyncio.sleep", new_callable=AsyncMock):
                segments, transcript = asyncio.run(transcribe_audio(audio_path))

        self.assertEqual(client.starts, 1)
        self.assertEqual(client.result_reads, 2)
        self.assertEqual(segments[0].text, "Текст")
        self.assertEqual(transcript, "[Врач] Текст")


class ProcessingStateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_record(self, record_id, status, attempts):
        row = Consultation(
            id=record_id,
            consultation_date=date(2026, 9, 24),
            consultation_type="repeat_adult",
            clinic_division="Петра",
            doctor_name="Врач",
            patient_name="Пациент",
            audio_path="/tmp/audio.mp3",
            original_filename="audio.mp3",
            status=status,
            processing_attempts=attempts,
            processing_generation=0,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_manual_retry_after_attempt_limit_ignores_old_messages(self):
        row = self.add_record("failed-record", "failed", 2)
        generation = reset_for_manual_retry(row)
        self.db.commit()

        self.assertEqual(generation, 1)
        self.assertFalse(claim_processing(self.db, row.id, 0))
        self.assertTrue(claim_processing(self.db, row.id, generation))
        self.assertFalse(claim_processing(self.db, row.id, generation))
        self.db.refresh(row)
        self.assertEqual(row.processing_attempts, 1)

    def test_recovery_invalidates_old_tasks_and_preserves_real_attempt_count(self):
        self.add_record("interrupted", "processing", 1)
        self.add_record("queued", "uploaded", 0)
        self.add_record("exhausted", "processing", 2)

        pending = dict(recover_pending(self.db))
        self.assertEqual(pending, {"interrupted": 1, "queued": 1})
        for record_id in ("interrupted", "queued"):
            self.assertFalse(claim_processing(self.db, record_id, 0))
            self.assertTrue(claim_processing(self.db, record_id, 1))
            self.assertFalse(claim_processing(self.db, record_id, 1))

        exhausted = self.db.get(Consultation, "exhausted")
        self.assertEqual(exhausted.status, "failed")
        generation = reset_for_manual_retry(exhausted)
        self.db.commit()
        self.assertTrue(claim_processing(self.db, exhausted.id, generation))
        self.assertEqual(exhausted.processing_attempts, 1)

    def test_evaluation_retry_reuses_saved_transcript(self):
        row = self.add_record("evaluation-record", "uploaded", 0)
        self.assertTrue(claim_processing(self.db, row.id, 0))
        with tempfile.TemporaryDirectory() as temporary_dir:
            audio_path = Path(temporary_dir) / "audio.mp3"
            audio_path.write_bytes(b"fake audio")
            row.audio_path = str(audio_path)
            self.db.commit()

            async def transcribe(_path):
                return [TranscriptSegment(speaker_role="doctor", start_ms=0, end_ms=1000,
                                          text="Добрый день", order_index=0)], "[Врач] Добрый день"

            async def fail_evaluation(_text, _consultation_type):
                raise RuntimeError("temporary YandexGPT error")

            with patch("app.services.pipeline.SessionLocal", self.Session), \
                 patch("app.services.pipeline.transcribe_audio", transcribe), \
                 patch("app.services.pipeline.evaluate_transcript", fail_evaluation), \
                 patch("app.services.pipeline.get_duration_sec", return_value=1):
                asyncio.run(process_consultation(row.id, 0))

            self.db.refresh(row)
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.transcript_text, "[Врач] Добрый день")
            self.assertEqual(len(row.segments), 1)

            generation = reset_for_manual_retry(row)
            self.db.commit()
            self.assertTrue(claim_processing(self.db, row.id, generation))

            async def evaluate(_text, _consultation_type):
                return "Отчёт", 4.0

            with patch("app.services.pipeline.SessionLocal", self.Session), \
                 patch("app.services.pipeline.transcribe_audio", side_effect=AssertionError("STT called twice")), \
                 patch("app.services.pipeline.evaluate_transcript", evaluate):
                asyncio.run(process_consultation(row.id, generation))

            self.db.refresh(row)
            self.assertEqual(row.status, "ready")
            self.assertEqual(row.evaluation_report, "Отчёт")
            self.assertEqual(len(row.segments), 1)

    def test_stale_transcription_cannot_overwrite_manual_retry(self):
        row = self.add_record("stale-record", "uploaded", 0)
        self.assertTrue(claim_processing(self.db, row.id, 0))
        with tempfile.TemporaryDirectory() as temporary_dir:
            audio_path = Path(temporary_dir) / "audio.mp3"
            audio_path.write_bytes(b"fake audio")
            row.audio_path = str(audio_path)
            self.db.commit()

            async def transcribe(_path):
                with self.Session() as concurrent_db:
                    concurrent_row = concurrent_db.get(Consultation, row.id)
                    reset_for_manual_retry(concurrent_row)
                    concurrent_db.commit()
                return [TranscriptSegment(speaker_role="doctor", start_ms=0, end_ms=1000,
                                          text="Старый текст", order_index=0)], "Старый текст"

            with patch("app.services.pipeline.SessionLocal", self.Session), \
                 patch("app.services.pipeline.transcribe_audio", transcribe), \
                 patch("app.services.pipeline.get_duration_sec", return_value=1):
                asyncio.run(process_consultation(row.id, 0))

            self.db.refresh(row)
            self.assertEqual(row.processing_generation, 1)
            self.assertEqual(row.status, "uploaded")
            self.assertIsNone(row.transcript_text)
            self.assertEqual(len(row.segments), 0)


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
        with engine.connect() as connection:
            generation = connection.scalar(text("SELECT processing_generation FROM consultations WHERE id = 'old-record'"))
        self.assertEqual(generation, 0)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
