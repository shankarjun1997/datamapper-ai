"""initial schema — xref sessions, audit events, mapping memory

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-28 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "xref_sessions",
        sa.Column("id",           sa.String(length=36), primary_key=True),
        sa.Column("tenant",       sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("name",         sa.String(length=255), server_default=""),
        sa.Column("status",       sa.String(length=32),  server_default="new"),
        sa.Column("stage",        sa.String(length=32),  server_default="idle"),
        sa.Column("created_at",   sa.String(length=32),  server_default=""),
        sa.Column("filename",     sa.String(length=255), server_default=""),
        sa.Column("instructions", sa.Text(),             server_default=""),
        sa.Column("mappings",         sa.JSON(), nullable=True),
        sa.Column("stats",            sa.JSON(), nullable=True),
        sa.Column("bq_config",        sa.JSON(), nullable=True),
        sa.Column("api_config",       sa.JSON(), nullable=True),
        sa.Column("usage",            sa.JSON(), nullable=True),
        sa.Column("src_columns",      sa.JSON(), nullable=True),
        sa.Column("tgt_columns",      sa.JSON(), nullable=True),
        sa.Column("table_mappings",   sa.JSON(), nullable=True),
        sa.Column("jira_context",     sa.JSON(), nullable=True),
        sa.Column("mapping_versions", sa.JSON(), nullable=True),
        sa.Column("extra",            sa.JSON(), nullable=True),
    )
    op.create_index("ix_xref_sessions_tenant", "xref_sessions", ["tenant"])

    op.create_table(
        "xref_audit_events",
        sa.Column("id",         sa.String(length=36), primary_key=True),
        sa.Column("ts",         sa.String(length=32)),
        sa.Column("event",      sa.String(length=64)),
        sa.Column("tenant",     sa.String(length=64)),
        sa.Column("email",      sa.String(length=255)),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("ip",         sa.String(length=64), server_default="unknown"),
        sa.Column("meta",       sa.JSON(), nullable=True),
    )
    op.create_index("ix_xref_audit_events_ts",     "xref_audit_events", ["ts"])
    op.create_index("ix_xref_audit_events_event",  "xref_audit_events", ["event"])
    op.create_index("ix_xref_audit_events_tenant", "xref_audit_events", ["tenant"])
    op.create_index("ix_xref_audit_events_email",  "xref_audit_events", ["email"])

    op.create_table(
        "xref_mapping_memory",
        sa.Column("src_field",        sa.String(length=255), primary_key=True),
        sa.Column("tgt_table",        sa.String(length=255), server_default=""),
        sa.Column("tgt_column",       sa.String(length=255), server_default=""),
        sa.Column("mapping_type",     sa.String(length=32),  server_default="Direct"),
        sa.Column("mapping_relation", sa.String(length=8),   server_default="1:1"),
        sa.Column("business_logic",   sa.Text(),             server_default=""),
        sa.Column("confidence",       sa.Float(),            server_default="0.5"),
        sa.Column("uses",             sa.Integer(),          server_default="1"),
        sa.Column("last_updated",     sa.String(length=32),  server_default=""),
        sa.Column("user_override",    sa.Boolean(),          server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_table("xref_mapping_memory")
    op.drop_index("ix_xref_audit_events_email", table_name="xref_audit_events")
    op.drop_index("ix_xref_audit_events_tenant", table_name="xref_audit_events")
    op.drop_index("ix_xref_audit_events_event", table_name="xref_audit_events")
    op.drop_index("ix_xref_audit_events_ts", table_name="xref_audit_events")
    op.drop_table("xref_audit_events")
    op.drop_index("ix_xref_sessions_tenant", table_name="xref_sessions")
    op.drop_table("xref_sessions")
