import asyncio
import random
import re

import httpx

from app.config import get_settings
from app.services.mock_ai import mock_evaluate


def _load_prompt(consultation_type: str) -> str:
    settings = get_settings()
    prompt_paths = {
        "primary_adult": settings.evaluation_prompt_primary_path,
        "primary_child": settings.evaluation_prompt_primary_child_path,
        "repeat_adult": settings.evaluation_prompt_repeat_path,
    }
    path = prompt_paths.get(consultation_type, settings.evaluation_prompt_primary_path)
    return path.read_text(encoding="utf-8")


def _parse_overall_score(report: str) -> float | None:
    match = re.search(r"Общий балл:\s*([\d.,]+)", report)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _is_moderation_refusal(data: dict, alternative: dict, report: str) -> bool:
    details = data.get("incomplete_details") or data.get("incompleteDetails") or {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    result_details = result.get("incomplete_details") or result.get("incompleteDetails") or {}
    reason = details.get("reason") or result_details.get("reason")
    status = alternative.get("status") or data.get("status") or result.get("status")
    normalized_report = report.replace("ё", "е").strip().lower()
    return (
        reason == "content_filter"
        or status in {"incomplete", "ALTERNATIVE_STATUS_CONTENT_FILTER"}
        or normalized_report.startswith("я не могу обсуждать эту тему")
    )


async def evaluate_transcript(transcript: str, consultation_type: str = "primary_adult") -> tuple[str, float | None]:
    settings = get_settings()
    if settings.mock_ai:
        report, score = mock_evaluate(transcript)
        return report, score

    if not settings.yandex_api_key or not settings.yandex_folder_id:
        raise RuntimeError("Задайте YANDEX_API_KEY и YANDEX_FOLDER_ID или включите MOCK_AI=true")

    system_prompt = _load_prompt(consultation_type)
    user_message = f"Транскрипция консультации:\n\n{transcript}"

    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Authorization": f"Api-Key {settings.yandex_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "modelUri": f"gpt://{settings.yandex_folder_id}/{settings.yandexgpt_model}",
        "completionOptions": {
            "stream": False,
            "temperature": 0.3,
            "maxTokens": 8000,
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_message},
        ],
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = None
        for attempt in range(4):
            try:
                response = await client.post(url, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 3:
                    raise RuntimeError(
                        f"YandexGPT network request failed after retries ({type(exc).__name__})"
                    ) from exc
                await asyncio.sleep(min(2 ** attempt + random.random(), 8))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(min(2 ** attempt + random.random(), 8))
                    continue
            if response.is_error:
                detail = response.text.strip().replace("\n", " ")[:1000]
                raise RuntimeError(f"YandexGPT HTTP {response.status_code}: {detail or response.reason_phrase}")
            break
        if response is None:
            raise RuntimeError("YandexGPT request retries exhausted")
        data = response.json()

    alternative = data["result"]["alternatives"][0]
    report = alternative["message"]["text"]
    if _is_moderation_refusal(data, alternative, report):
        raise RuntimeError(
            "YandexGPT: оценка заблокирована модерацией content_filter. "
            "Нужна настройка правил модерации/инстанса в AI Studio или повторная обработка с другим YandexGPT-инстансом."
        )
    return report, _parse_overall_score(report)


def split_transcript(transcript: str, max_chars: int = 9000) -> list[str]:
    """Split on dialogue lines so each model request stays bounded."""
    chunks: list[str] = []
    current = ""
    for line in transcript.splitlines():
        if len(current) + len(line) + 1 > max_chars and current:
            chunks.append(current)
            current = ""
        while len(line) > max_chars:
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks or [transcript]


async def summarize_transcript_chunk(chunk: str, index: int, total: int) -> str:
    settings = get_settings()
    if settings.mock_ai:
        return f"Часть {index + 1}/{total}: {chunk[:1500]}"
    if not settings.yandex_api_key or not settings.yandex_folder_id:
        raise RuntimeError("Задайте YANDEX_API_KEY и YANDEX_FOLDER_ID")
    body = {
        "modelUri": f"gpt://{settings.yandex_folder_id}/{settings.yandexgpt_model}",
        "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": 1400},
        "messages": [
            {"role": "system", "text": (
                "Извлеки только наблюдаемые факты и короткие дословные цитаты для оценки врача "
                "по этапам: контакт, анамнез, диагностика, презентация лечения, возражения, завершение. "
                "Для отсутствующих этапов напиши 'нет данных'. Не выставляй баллы и не додумывай факты. "
                "Ответ до 1600 символов."
            )},
            {"role": "user", "text": f"Часть {index + 1} из {total}:\n{chunk}"},
        ],
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {settings.yandex_api_key}"}, json=body,
        )
        response.raise_for_status()
        data = response.json()
    alternative = data["result"]["alternatives"][0]
    summary = alternative["message"]["text"]
    if _is_moderation_refusal(data, alternative, summary):
        raise RuntimeError("YandexGPT: фрагмент отклонён модерацией")
    return summary[:1800]


async def evaluate_from_summaries(summaries: list[str], consultation_type: str) -> tuple[str, float | None]:
    evidence = "\n\n".join(f"Часть {i + 1}: {summary}" for i, summary in enumerate(summaries))
    return await evaluate_transcript(
        "Сжатые факты по последовательным частям консультации. Отсутствие этапа в отдельной части "
        "не означает отсутствия этапа во всём разговоре. Цитируй только приведённые дословные фразы.\n\n"
        + evidence, consultation_type,
    )
