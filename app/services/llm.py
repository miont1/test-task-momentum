import asyncio
import logging
import random
import re
import time

import redis.asyncio as aioredis
from openai import AsyncOpenAI

from app.config import settings
from app.models import KnowledgeArticle

logger = logging.getLogger(__name__)


def _is_cyrillic(text: str) -> bool:
    """Return True if the text contains Cyrillic characters (e.g., Ukrainian)."""
    return bool(re.search(r"[а-яіїєґА-ЯІЇЄҐ]", text))


def _strip_cyrillic_annotations(text: str) -> str:
    """Remove any parenthetical Cyrillic annotations when formatting English replies."""
    return re.sub(r"\s*\([^)]*[а-яіїєґА-ЯІЇЄҐ][^)]*\)", "", text).strip()


async def acquire_llm_rate_limit() -> None:
    """Enforce a global 60 requests-per-minute ceiling across the application via Redis.

    Gracefully bypasses if Redis is unavailable (e.g., in isolated unit tests).
    """
    limit = settings.llm_rate_limit_per_minute
    if limit <= 0:
        return

    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        return

    try:
        while True:
            current_minute = int(time.time() // 60)
            key = f"llm:rate_limit:{current_minute}"
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, 65)
            if count <= limit:
                return
            sleep_for = max(0.5, 60.0 - (time.time() % 60)) + random.uniform(0.05, 1.25)
            logger.warning(
                "LLM rate limit (%d/min) reached; throttling for %.2fs",
                limit,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
    except Exception as exc:
        logger.debug("Redis rate limit check bypassed: %s", exc)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def _build_mock_draft(
    customer_message: str,
    articles: list[KnowledgeArticle],
) -> str:
    """Build a deterministic, grounded reply citing matched KnowledgeArticle records."""
    use_ukrainian = _is_cyrillic(customer_message)

    if not articles:
        if use_ukrainian:
            return (
                "Вітаємо!\n\n"
                "Дякуємо за звернення до служби підтримки CloudMetrics. Ми отримали "
                "ваш запит і передали його спеціалісту. Будь ласка, надайте додаткові "
                "деталі (наприклад, назву робочого простору або кроки для відтворення), "
                "щоб ми могли допомогти вам якнайшвидше.\n\n"
                "З повагою,\n"
                "Служба підтримки CloudMetrics"
            )
        return (
            "Hello,\n\n"
            "Thank you for contacting CloudMetrics Support. We received your inquiry, "
            "and a support specialist is reviewing the details of your request. Could "
            "you please share any additional context (such as workspace name or steps "
            "to reproduce) so we can assist you as quickly as possible?\n\n"
            "Best regards,\n"
            "CloudMetrics Support"
        )

    primary = articles[0]
    primary_title = (
        primary.title if use_ukrainian else _strip_cyrillic_annotations(primary.title)
    )
    primary_content = (
        primary.content
        if use_ukrainian
        else _strip_cyrillic_annotations(primary.content)
    )
    citations = "\n".join(
        f"- {art.title if use_ukrainian else _strip_cyrillic_annotations(art.title)}: {art.url}"
        for art in articles
    )
    additional_notes = ""
    if len(articles) > 1:
        extra_summaries = " ".join(
            art.content if use_ukrainian else _strip_cyrillic_annotations(art.content)
            for art in articles[1:]
        )
        if use_ukrainian:
            additional_notes = f"\n\nДодаткова інформація: {extra_summaries}"
        else:
            additional_notes = f"\n\nAdditional context: {extra_summaries}"

    if use_ukrainian:
        return (
            "Вітаємо!\n\n"
            "Дякуємо за звернення до служби підтримки CloudMetrics! Відповідно до "
            f"статті нашої бази знань \"{primary_title}\", ось інформація щодо "
            "вашого запиту:\n\n"
            f"{primary_content}{additional_notes}\n\n"
            "Корисні посилання:\n"
            f"{citations}\n\n"
            "Будь ласка, повідомте, якщо вам знадобиться додаткова допомога!\n\n"
            "З повагою,\n"
            "Служба підтримки CloudMetrics"
        )

    return (
        "Hello,\n\n"
        "Thank you for reaching out to CloudMetrics Support! Based on our knowledge "
        f"base article \"{primary_title}\", here is how to resolve your question:\n\n"
        f"{primary_content}{additional_notes}\n\n"
        "Reference Documentation:\n"
        f"{citations}\n\n"
        "Please let us know if you need any further assistance!\n\n"
        "Best regards,\n"
        "CloudMetrics Support"
    )


async def generate_draft_reply(
    customer_message: str,
    articles: list[KnowledgeArticle],
) -> str:
    """Generate a customer support draft reply using either Mock mode or OpenAI."""
    await acquire_llm_rate_limit()

    api_key = settings.effective_api_key
    if api_key == "mock":
        delay = random.uniform(5.0, 30.0)
        logger.info(
            "LLM mock mode active: simulating provider latency of %.2fs (matched_articles=%d)",
            delay,
            len(articles),
        )
        await asyncio.sleep(delay)
        return _build_mock_draft(customer_message=customer_message, articles=articles)

    if articles:
        kb_context = "\n\n".join(
            f"Title: {_strip_cyrillic_annotations(art.title)}\n"
            f"URL: {art.url}\n"
            f"Content: {_strip_cyrillic_annotations(art.content)}"
            for art in articles
        )
    else:
        kb_context = "No matching knowledge base articles found."

    system_prompt = (
        "You are a helpful, accurate customer support assistant for CloudMetrics. "
        "Draft a concise, polite response in the same language as the customer's "
        "inquiry, grounded strictly in the provided Knowledge Base articles. Always "
        "include the matched article title and exact URL as citations in your reply. "
        "If no articles match, politely ask the customer for clarifying details "
        "without inventing product features or URLs."
    )
    user_prompt = (
        f"Customer Inquiry:\n{customer_message}\n\n"
        f"Knowledge Base Context:\n{kb_context}"
    )

    model_name = settings.openai_model.strip()
    request_kwargs: dict = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if not model_name.lower().startswith(("gpt-5", "o1", "o3", "o4")):
        request_kwargs["temperature"] = 0.2

    async with AsyncOpenAI(api_key=api_key) as client:
        response = await client.chat.completions.create(**request_kwargs)
        content = response.choices[0].message.content
        return (content or "").strip() or _build_mock_draft(customer_message, articles)
