import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert

from app.database import AsyncSessionLocal
from app.models import KnowledgeArticle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INITIAL_ARTICLES = [
    {
        "title": "Exporting Reports to PDF/CSV (Експорт звітів у PDF / CSV, аналітика)",
        "url": "https://help.cloudmetrics.io/articles/export-pdf-csv",
        "content": (
            "Instructions on navigating to Analytics > Reports, selecting date ranges, "
            "and clicking Export. Mentions 50,000 row limits. "
            "(Експорт звітів у PDF / CSV, аналітика, експортувати звіт)"
        ),
    },
    {
        "title": "Two-Factor Authentication (2FA) Setup (Двофакторна автентифікація, безпека, код відновлення)",
        "url": "https://help.cloudmetrics.io/articles/2fa-setup",
        "content": (
            "Guide on enabling 2FA under Settings > Security using authenticator apps "
            "and emergency recovery codes. "
            "(Двофакторна автентифікація, безпека, код відновлення)"
        ),
    },
    {
        "title": "User Roles and Workspace Permissions (Ролі та права доступу, адміністратор, учасники команди)",
        "url": "https://help.cloudmetrics.io/articles/roles-permissions",
        "content": (
            "Details on Admin, Editor, and Viewer roles. Only workspace owners manage billing. "
            "(Ролі та права доступу, адміністратор, учасники команди)"
        ),
    },
    {
        "title": "Webhook Delivery Failures & Retries (Вебхуки, збої доставки, таймаути, API ключі)",
        "url": "https://help.cloudmetrics.io/articles/webhooks-retry",
        "content": (
            "Explains the 2-second timeout, 3 automated retry attempts, and 15-minute pause "
            "after persistent 5xx. "
            "(Вебхуки, збої доставки, таймаути, API ключі)"
        ),
    },
    {
        "title": "Subscription Billing & Invoices (Оплата, підписка, повернення коштів, рахунки)",
        "url": "https://help.cloudmetrics.io/articles/billing-invoices",
        "content": (
            "Explains monthly renewal cycles, prorated upgrades, and invoice downloads. "
            "(Оплата, підписка, повернення коштів, рахунки)"
        ),
    },
]


async def seed_knowledge_base() -> None:
    """Idempotently seed the knowledge_articles table with CloudMetrics articles."""
    async with AsyncSessionLocal() as session:
        stmt = insert(KnowledgeArticle).values(INITIAL_ARTICLES)
        stmt = stmt.on_conflict_do_update(
            index_elements=[KnowledgeArticle.url],
            set_={
                "title": stmt.excluded.title,
                "content": stmt.excluded.content,
            },
        )
        await session.execute(stmt)
        await session.commit()
        logger.info("Seeded %d knowledge base articles successfully.", len(INITIAL_ARTICLES))


if __name__ == "__main__":
    asyncio.run(seed_knowledge_base())
