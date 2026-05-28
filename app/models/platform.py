"""
app/models/platform.py — SQLAlchemy models for sessions, audit events,
and cross-session mapping memory.

Heavy/variable payloads are kept as JSON columns so the schema stays
small and stable while routers/agents evolve. The ``extra`` column on
DBSession is a catch-all for fields not explicitly modelled.
"""
from __future__ import annotations

from sqlalchemy import Column, String, Float, Integer, Boolean, Text, JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class DBSession(Base):
    __tablename__ = "xref_sessions"

    id           = Column(String(36), primary_key=True)
    tenant       = Column(String(64), index=True, nullable=False, default="default")
    name         = Column(String(255), default="")
    status       = Column(String(32), default="new")
    stage        = Column(String(32), default="idle")
    created_at   = Column(String(32), default="")
    filename     = Column(String(255), default="")
    instructions = Column(Text, default="")

    # Heavy JSON blobs
    mappings         = Column(JSON, default=list)
    stats            = Column(JSON, default=dict)
    bq_config        = Column(JSON, default=dict)
    api_config       = Column(JSON, default=dict)
    usage            = Column(JSON, default=dict)
    src_columns      = Column(JSON, default=list)
    tgt_columns      = Column(JSON, default=list)
    table_mappings   = Column(JSON, default=list)
    jira_context     = Column(JSON, default=dict)
    mapping_versions = Column(JSON, default=list)

    # Catch-all blob for any field not explicitly modelled
    extra = Column(JSON, default=dict)


class DBAuditEvent(Base):
    __tablename__ = "xref_audit_events"

    id         = Column(String(36), primary_key=True)
    ts         = Column(String(32), index=True)
    event      = Column(String(64), index=True)
    tenant     = Column(String(64), index=True)
    email      = Column(String(255), index=True)
    session_id = Column(String(36), nullable=True)
    ip         = Column(String(64), default="unknown")
    meta       = Column(JSON, default=dict)


class DBMappingMemory(Base):
    __tablename__ = "xref_mapping_memory"

    src_field        = Column(String(255), primary_key=True)
    tgt_table        = Column(String(255), default="")
    tgt_column       = Column(String(255), default="")
    mapping_type     = Column(String(32), default="Direct")
    mapping_relation = Column(String(8), default="1:1")
    business_logic   = Column(Text, default="")
    confidence       = Column(Float, default=0.5)
    uses             = Column(Integer, default=1)
    last_updated     = Column(String(32), default="")
    user_override    = Column(Boolean, default=False)
