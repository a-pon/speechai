import asyncio
import base64
import json
from pathlib import Path

import httpx

from app.config import get_settings
from app.models import TranscriptSegment
from app.services.audio_utils import get_audio_channels
from app.services.mock_ai import mock_transcribe

STT_URL = "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
GET_RECOGNITION_URL = "https://stt.api.cloud.yandex.net/stt/v3/getRecognition"
OPS_URL = "https://operation.api.cloud.yandex.net/operations"


def _audio_container_type(path: Path) -> str:
    ext = path.suffix.lower()
    mapping = {".mp3": "MP3", ".wav": "WAV", ".ogg": "OGG_OPUS", ".opus": "OGG_OPUS"}
    return mapping.get(ext, "MP3")


def _headers(*, json_body: bool = True) -> dict[str, str]:
    settings = get_settings()
    headers = {"Authorization": f"Api-Key {settings.yandex_api_key}"}
    if settings.yandex_folder_id:
        headers["x-folder-id"] = settings.yandex_folder_id
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _ms(value: str | int | float | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _channel_index(raw: str | int | None) -> int:
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _parse_ndjson(text: str) -> list[dict]:
    """SpeechKit getRecognition отдаёт несколько JSON-объектов подряд."""
    text = text.strip()
    if not text:
        return []
    objects: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        objects.append(obj)
        idx = end
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
    return objects


def _extract_from_alternative(
    alt: dict,
    channel: int,
    order: int,
) -> TranscriptSegment | None:
    text = (alt.get("text") or "").strip()
    if not text:
        return None
    start = _ms(alt.get("startTimeMs") or alt.get("start_time_ms"))
    end = _ms(alt.get("endTimeMs") or alt.get("end_time_ms"), start + 1000)
    role = "doctor" if channel == 0 else "patient"
    return TranscriptSegment(
        speaker_role=role,
        start_ms=start,
        end_ms=end,
        text=text,
        order_index=order,
    )


def _parse_recognition_events(events: list[dict]) -> list[TranscriptSegment]:
    """Собирает сегменты из потока getRecognition (final / finalRefinement)."""

    by_key: dict[tuple[int, str], TranscriptSegment] = {}
    order = 0

    for envelope in events:
        payload = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
        if not isinstance(payload, dict):
            continue
        channel = _channel_index(payload.get("channelTag") or payload.get("channel_tag"))
        final = payload.get("final")
        if isinstance(final, dict):
            channel = _channel_index(final.get("channelTag") or channel)
            final_index = str(final.get("finalIndex") or payload.get("audioCursors", {}).get("finalIndex") or order)
            alts = final.get("alternatives") or []
            if alts:
                seg = _extract_from_alternative(alts[0], channel, order)
                if seg:
                    by_key[(channel, final_index)] = seg
                    order += 1

        refinement = payload.get("finalRefinement")
        if isinstance(refinement, dict):
            final_index = str(refinement.get("finalIndex") or "0")
            normalized = refinement.get("normalizedText") or {}
            alts = normalized.get("alternatives") or []
            if alts:
                ch = _channel_index(normalized.get("channelTag") or channel)
                seg = _extract_from_alternative(alts[0], ch, order)
                if seg:
                    by_key[(ch, final_index)] = seg

    segments = list(by_key.values())
    segments.sort(key=lambda s: (s.start_ms, s.order_index))
    for i, seg in enumerate(segments):
        seg.order_index = i
    return segments


def _parse_stt_response(payload: dict) -> list[TranscriptSegment]:
    """Парсинг вложенного response (v2 / старые форматы), если есть chunks."""
    segments: list[TranscriptSegment] = []
    order = 0

    def add_segment(text: str, start_ms: int, end_ms: int, channel: int) -> None:
        nonlocal order
        if not text.strip():
            return
        role = "doctor" if channel == 0 else "patient"
        segments.append(
            TranscriptSegment(
                speaker_role=role,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text.strip(),
                order_index=order,
            )
        )
        order += 1

    for chunk in payload.get("chunks", []) or []:
        alts = chunk.get("alternatives") or []
        text = alts[0].get("text", "") if alts else chunk.get("text", "")
        start = _ms(chunk.get("startTimeMs") or chunk.get("start_time_ms"))
        end = _ms(chunk.get("endTimeMs") or chunk.get("end_time_ms"), start + 1000)
        channel = _channel_index(chunk.get("channelTag") or chunk.get("channel_tag"))
        add_segment(text, start, end, channel)

    result = payload.get("response") or payload.get("result") or {}
    if isinstance(result, dict):
        for item in result.get("chunks", []) or []:
            alts = item.get("alternatives") or []
            text = alts[0].get("text", "") if alts else ""
            channel = _channel_index(item.get("channelTag"))
            add_segment(text, 0, 1000, channel)

    return segments


async def _fetch_recognition_results(client: httpx.AsyncClient, operation_id: str) -> list[dict]:
    resp = await client.get(
        GET_RECOGNITION_URL,
        headers=_headers(json_body=False),
        params={"operation_id": operation_id},
    )
    resp.raise_for_status()
    return _parse_ndjson(resp.text)


async def transcribe_audio(audio_path: Path) -> tuple[list[TranscriptSegment], str]:
    settings = get_settings()
    if settings.mock_ai:
        return mock_transcribe(audio_path)

    if not settings.yandex_api_key:
        raise RuntimeError("Задайте YANDEX_API_KEY или включите MOCK_AI=true")

    content_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    container = _audio_container_type(audio_path)
    channels = get_audio_channels(audio_path)
    # Speaker labeling (диаризация) — только для моно. Стерео: канал 0/1 ≈ врач/пациент.
    use_speaker_labeling = channels == 1

    body: dict = {
        "content": content_b64,
        "recognitionModel": {
            "model": "general",
            "audioFormat": {"containerAudio": {"containerAudioType": container}},
            "audioProcessingType": "FULL_DATA",
        },
    }
    if use_speaker_labeling:
        body["speakerLabeling"] = {"speakerLabeling": "SPEAKER_LABELING_ENABLED"}

    async with httpx.AsyncClient(timeout=300.0) as client:
        start_resp = await client.post(STT_URL, headers=_headers(), json=body)
        start_resp.raise_for_status()
        operation_id = start_resp.json().get("id") or start_resp.json().get("operationId")
        if not operation_id:
            raise RuntimeError(f"SpeechKit: нет operation id: {start_resp.text}")

        for _ in range(120):
            await asyncio.sleep(5)
            op_resp = await client.get(f"{OPS_URL}/{operation_id}", headers=_headers(json_body=False))
            op_resp.raise_for_status()
            op_data = op_resp.json()
            if not op_data.get("done"):
                continue
            if op_data.get("error"):
                raise RuntimeError(str(op_data["error"]))

            events = await _fetch_recognition_results(client, operation_id)
            segments = _parse_recognition_events(events)

            if not segments:
                segments = _parse_stt_response(op_data.get("response") or op_data)

            if not segments:
                raise RuntimeError(
                    "SpeechKit: пустой результат распознавания. "
                    "Проверьте getRecognition и формат ответа."
                )

            lines = []
            for s in segments:
                label = "Врач" if s.speaker_role == "doctor" else "Пациент"
                lines.append(f"[{label}] {s.text}")
            return segments, "\n".join(lines)

        raise TimeoutError("SpeechKit: превышено время ожидания распознавания")
