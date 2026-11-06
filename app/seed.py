import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.models import KnowledgeArticle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INITIAL_ARTICLES = [
    {
        "title": "Exporting Reports to PDF/CSV",
        "url": "https://help.cloudmetrics.io/articles/export-pdf-csv",
        "content": (
            "Instructions on navigating to Analytics > Reports, selecting date ranges, "
            "and clicking Export. Mentions 50,000 row limits."
        ),
    },
    {
        "title": "Two-Factor Authentication (2FA) Setup",
        "url": "https://help.cloudmetrics.io/articles/2fa-setup",
        "content": (
            "Guide on enabling 2FA under Settings > Security using authenticator apps "
            "and emergency recovery codes."
        ),
    },
    {
        "title": "User Roles and Workspace Permissions",
        "url": "https://help.cloudmetrics.io/articles/roles-permissions",
        "content": (
            "Details on Admin, Editor, and Viewer roles. Only workspace owners manage billing."
        ),
    },
    {
        "title": "Webhook Delivery Failures & Retries",
        "url": "https://help.cloudmetrics.io/articles/webhooks-retry",
        "content": (
            "Explains the 2-second timeout, 3 automated retry attempts, and 15-minute pause "
            "after persistent 5xx."
        ),
    },
    {
        "title": "Subscription Billing & Invoices",
        "url": "https://help.cloudmetrics.io/articles/billing-invoices",
        "content": (
            "Explains monthly renewal cycles, prorated upgrades, and invoice downloads."
        ),
    },
]


async def seed_knowledge_base() -> None:
    """Idempotently seed the knowledge_articles table with CloudMetrics articles."""
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

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
