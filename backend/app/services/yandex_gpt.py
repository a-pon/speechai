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
    match = re.search(r"Общий балл(?:\*\*)?\s*:(?:\*\*)?\s*([\d.,]+)", report, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _score_report(report: str) -> tuple[str, float | None]:
    """Use the six stage grades when present; their mean is the contract for the total."""
    scores = [float(value.replace(",", ".")) for value in re.findall(
        r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Оценка(?:\*\*)?\s*:(?:\*\*)?\s*(?:\*\*)?([1-5](?:[.,]\d+)?)(?!\d)",
        report,
    )]
    if len(scores) == 6 and all(1 <= score <= 5 for score in scores):
        score = round(sum(scores) / 6, 2)
        total = re.compile(r"(Общий балл(?:\*\*)?\s*:(?:\*\*)?\s*(?:\*\*)?)([\d.,]+)", re.IGNORECASE)
        if total.search(report):
            report = total.sub(lambda match: match.group(1) + f"{score:.2f}", report, count=1)
        else:
            report = report.rstrip() + f"\n\nОбщий балл: {score:.2f} из 5."
        return report, score
    score = _parse_overall_score(report)
    return report, score if score is not None and 1 <= score <= 5 else None


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
        for format_attempt in range(2):
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
            report, score = _score_report(report)
            if score is not None:
                return report, score
            if format_attempt == 0:
                body["messages"][1]["text"] = (
                    user_message + "\n\nПовтори отчёт строго по шаблону. Для каждого из шести этапов "
                    "напиши отдельную строку «Оценка: N», где N — число от 1 до 5. "
                    "Добавь строку «Общий балл: N», равную среднему шести оценок. "
                    "Пиши кратко, чтобы весь отчёт поместился в ответ."
                )
    raise RuntimeError("YandexGPT: отчёт не содержит корректных баллов по этапам или общего балла")


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
