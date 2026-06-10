"""tenants + secret vault table

Revision ID: 0002_tenants_table
Revises: 0001_initial_schema
Create Date: 2026-06-08 00:00:00

Moves tenant accounts (users[] with bcrypt hashes) and the per-tenant secret
vault out of the local JSON file and into Postgres, so credentials survive
redeploys and stay consistent across instances.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_tenants_table"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "xref_tenants",
        sa.Column("slug",       sa.String(length=64), primary_key=True),
        sa.Column("name",       sa.String(length=255), server_default=""),
        sa.Column("plan",       sa.String(length=32),  server_default="trial"),
        sa.Column("data",       sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.String(length=32), server_default=""),
    )


def downgrade() -> None:
    op.drop_table("xref_tenants")
