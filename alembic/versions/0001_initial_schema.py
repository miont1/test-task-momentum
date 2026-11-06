"""Initial schema for tickets, webhook_events, and knowledge_articles.

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-11-06 10:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ticket_status_enum = sa.Enum(
    "pending_draft",
    "draft_ready",
    "rejected",
    "sending",
    "sent",
    "failed",
    name="ticket_status",
)


def upgrade() -> None:
    op.create_table(
        "knowledge_articles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index(
        op.f("ix_knowledge_articles_title"),
        "knowledge_articles",
        ["title"],
        unique=False,
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("customer_email", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply_url", sa.String(length=512), nullable=False),
        sa.Column("draft_reply", sa.Text(), nullable=True),
        sa.Column(
            "status",
            ticket_status_enum,
            server_default="pending_draft",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tickets_status"), "tickets", ["status"], unique=False)

    op.create_table(
        "webhook_events",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("ticket_id", sa.String(length=128), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_webhook_events_ticket_id"),
        "webhook_events",
        ["ticket_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_webhook_events_ticket_id"), table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index(op.f("ix_tickets_status"), table_name="tickets")
    op.drop_table("tickets")
    ticket_status_enum.drop(op.get_bind(), checkfirst=True)
    op.drop_index(op.f("ix_knowledge_articles_title"), table_name="knowledge_articles")
    op.drop_table("knowledge_articles")

