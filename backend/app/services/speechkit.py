import asyncio
import json
import random
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx

from app.config import get_settings
from app.models import TranscriptSegment
from app.services.audio_utils import get_audio_channels

STT_URL = "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
GET_RECOGNITION_URL = "https://stt.api.cloud.yandex.net/stt/v3/getRecognition"
OPS_URL = "https://operation.api.cloud.yandex.net/operations"


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    content_factory: Callable[[], AsyncIterator[bytes]] | None = None,
    **kwargs,
) -> httpx.Response:
    """Retry transient network and Yandex throttling/server errors; surface useful provider diagnostics."""
    for attempt in range(4):
        try:
            request_kwargs = {**kwargs, "content": content_factory()} if content_factory else kwargs
            response = await client.request(method, url, **request_kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == 3:
                raise RuntimeError(
                    f"Yandex SpeechKit: network request failed after retries ({type(exc).__name__})"
                ) from exc
            await asyncio.sleep(min(2 ** attempt + random.random(), 8))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 3:
                await asyncio.sleep(min(2 ** attempt + random.random(), 8))
                continue
        if response.is_error:
            body = response.text.strip().replace("\n", " ")[:1000]
            raise RuntimeError(f"Yandex SpeechKit HTTP {response.status_code}: {body or response.reason_phrase}")
        return response
    raise RuntimeError("Yandex SpeechKit: request retries exhausted")


def _audio_container_type(path: Path) -> str:
    ext = path.suffix.lower()
    mapping = {".mp3": "MP3", ".wav": "WAV", ".ogg": "OGG_OPUS", ".opus": "OGG_OPUS"}
    return mapping.get(ext, "MP3")


def _headers(*, json_body: bool = True) -> dict[str, str]:
    settings = get_settings()
    headers = {"Authorization": f"Api-Key {settings.yandex_api_key}"}
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


def _get_any(payload: dict, *keys: str):
    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


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
        if isinstance(obj, list):
            objects.extend(item for item in obj if isinstance(item, dict))
        elif isinstance(obj, dict):
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
    raw_text = alt.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    words = [word for word in (alt.get("words") or []) if isinstance(word, dict)]
    if not text:
        text = " ".join(
            word["text"].strip() for word in words if isinstance(word.get("text"), str)
        )
    if not text:
        return None
    word_start = _ms(_get_any(words[0], "startTimeMs", "start_time_ms")) if words else 0
    start = _ms(_get_any(alt, "startTimeMs", "start_time_ms"), word_start)
    word_end = _ms(_get_any(words[-1], "endTimeMs", "end_time_ms"), start + 1000) if words else start + 1000
    end = _ms(_get_any(alt, "endTimeMs", "end_time_ms"), word_end)
    role = "doctor" if channel == 0 else "patient"
    return TranscriptSegment(
        speaker_role=role,
        start_ms=start,
        end_ms=end,
        text=text,
        order_index=order,
    )


def _first_segment(alternatives: list, channel: int, order: int) -> TranscriptSegment | None:
    for alternative in alternatives:
        if isinstance(alternative, dict):
            segment = _extract_from_alternative(alternative, channel, order)
            if segment:
                return segment
    return None


class RecognitionCollector:
    def __init__(self) -> None:
        self.by_key: dict[tuple[int, str], TranscriptSegment] = {}
        self.order = 0

    def add(self, envelope: dict) -> None:
        payload = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
        if not isinstance(payload, dict):
            return
        channel = _channel_index(_get_any(payload, "channelTag", "channel_tag"))
        audio_cursors = _get_any(payload, "audioCursors", "audio_cursors") or {}
        final = payload.get("final")
        if isinstance(final, dict):
            channel = _channel_index(_get_any(final, "channelTag", "channel_tag") or channel)
            final_index = str(
                _get_any(final, "finalIndex", "final_index")
                or _get_any(audio_cursors, "finalIndex", "final_index")
                or self.order
            )
            seg = _first_segment(final.get("alternatives") or [], channel, self.order)
            if seg:
                self.by_key[(channel, final_index)] = seg
                self.order += 1
        refinement = _get_any(payload, "finalRefinement", "final_refinement")
        if isinstance(refinement, dict):
            final_index = str(_get_any(refinement, "finalIndex", "final_index") or "0")
            normalized = _get_any(refinement, "normalizedText", "normalized_text") or {}
            ch = _channel_index(_get_any(normalized, "channelTag", "channel_tag") or channel)
            seg = _first_segment(normalized.get("alternatives") or [], ch, self.order)
            if seg:
                self.by_key[(ch, final_index)] = seg

    def segments(self) -> list[TranscriptSegment]:
        segments = list(self.by_key.values())
        segments.sort(key=lambda item: (item.start_ms, item.order_index))
        for index, segment in enumerate(segments):
            segment.order_index = index
        return segments


def _parse_recognition_events(events: list[dict]) -> list[TranscriptSegment]:
    collector = RecognitionCollector()
    for event in events:
        collector.add(event)
    return collector.segments()


async def _iter_recognition_events(client: httpx.AsyncClient, operation_id: str) -> AsyncIterator[dict]:
    """Parse a concatenated JSON/NDJSON response without materializing the full body."""
    import codecs

    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    async with client.stream("GET", GET_RECOGNITION_URL,
                             headers=_headers(json_body=False),
                             params={"operationId": operation_id}) as response:
        if response.is_error:
            body = (await response.aread()).decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Yandex SpeechKit HTTP {response.status_code}: {body}")
        async for chunk in response.aiter_bytes(65536):
            buffer += utf8.decode(chunk)
            while True:
                buffer = buffer.lstrip()
                if not buffer:
                    break
                try:
                    obj, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    if len(buffer) > 8 * 1024 * 1024:
                        raise RuntimeError("SpeechKit: слишком большой или некорректный JSON-объект результата")
                    break
                buffer = buffer[end:]
                if isinstance(obj, dict):
                    yield obj
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict):
                            yield item
    buffer += utf8.decode(b"", final=True)
    if buffer.strip():
        raise RuntimeError("SpeechKit: неполный JSON в результате распознавания")


def _recognition_diagnostics(events: list[dict]) -> tuple[int, int, int, int, str]:
    finals = refinements = texts = words = 0
    status_messages = []
    for envelope in events:
        payload = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
        status = _get_any(payload, "statusCode", "status_code")
        if isinstance(status, dict):
            code = _get_any(status, "codeType", "code_type") or "unknown"
            message = str(status.get("message") or "").replace("\n", " ")[:120]
            status_messages.append(f"{code}: {message}")
        final = payload.get("final")
        refinement = _get_any(payload, "finalRefinement", "final_refinement")
        if isinstance(final, dict):
            finals += 1
            alternatives = final.get("alternatives") or []
        elif isinstance(refinement, dict):
            refinements += 1
            normalized = _get_any(refinement, "normalizedText", "normalized_text") or {}
            alternatives = normalized.get("alternatives") or []
        else:
            continue
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                continue
            if isinstance(alternative.get("text"), str) and alternative["text"].strip():
                texts += 1
            words += sum(
                bool(word.get("text"))
                for word in (alternative.get("words") or [])
                if isinstance(word, dict)
            )
    return finals, refinements, texts, words, "; ".join(status_messages[-2:])


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


async def _fetch_recognition_results(client: httpx.AsyncClient, operation_id: str) -> tuple[list[dict], str]:
    resp = await _request_with_retry(
        client,
        "GET",
        GET_RECOGNITION_URL,
        headers=_headers(json_body=False),
        params={"operationId": operation_id},
    )
    return _parse_ndjson(resp.text), "operationId"


async def start_recognition(audio_uri: str, audio_path: Path) -> str:
    """Submit a private Object Storage URI and return the durable operation ID."""
    settings = get_settings()
    if not settings.yandex_api_key:
        raise RuntimeError("Задайте YANDEX_API_KEY")
    metadata = {
        "uri": audio_uri,
        "recognitionModel": {
            "model": "general",
            "audioFormat": {"containerAudio": {"containerAudioType": _audio_container_type(audio_path)}},
            "audioProcessingType": "FULL_DATA",
        },
    }
    if get_audio_channels(audio_path) == 1:
        metadata["speakerLabeling"] = {"speakerLabeling": "SPEAKER_LABELING_ENABLED"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        # POST is not idempotent: a lost response may already contain an accepted operation.
        response = await client.post(STT_URL, headers=_headers(), json=metadata)
    if response.is_error:
        body = response.text.strip().replace("\n", " ")[:1000]
        raise RuntimeError(f"SpeechKit HTTP {response.status_code}: {body or response.reason_phrase}")
    operation_id = response.json().get("id") or response.json().get("operationId")
    if not operation_id:
        raise RuntimeError("SpeechKit не вернул ID операции")
    return operation_id


async def poll_recognition(operation_id: str) -> tuple[bool, list[TranscriptSegment] | None]:
    """One short poll. Completed operations may need several result reads."""
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await _request_with_retry(
            client, "GET", f"{OPS_URL}/{operation_id}", headers=_headers(json_body=False)
        )
        operation = response.json()
        if not operation.get("done"):
            return False, None
        if operation.get("error"):
            raise RuntimeError(f"SpeechKit operation {operation_id}: {operation['error']}")
        collector = RecognitionCollector()
        async for event in _iter_recognition_events(client, operation_id):
            collector.add(event)
        segments = collector.segments()
        if not segments:
            segments = _parse_stt_response(operation.get("response") or operation)
        return True, segments or None


def format_transcript(segments: list[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{'Врач' if segment.speaker_role == 'doctor' else 'Пациент'}] {segment.text}"
        for segment in segments
    )
