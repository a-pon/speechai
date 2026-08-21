import re
from pathlib import Path

import httpx

from app.config import get_settings
from app.services.mock_ai import mock_evaluate


def _load_prompt(consultation_type: str) -> str:
    settings = get_settings()
    path = (
        settings.evaluation_prompt_repeat_path
        if consultation_type == "repeat_adult"
        else settings.evaluation_prompt_primary_path
    )
    return path.read_text(encoding="utf-8")


def _parse_overall_score(report: str) -> float | None:
    match = re.search(r"Общий балл:\s*([\d.,]+)", report)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


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
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()

    report = data["result"]["alternatives"][0]["message"]["text"]
    return report, _parse_overall_score(report)
