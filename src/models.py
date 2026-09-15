from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import DateTime, Integer, String, Text, JSON, LargeBinary, UniqueConstraint, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def now() -> datetime: return datetime.now(timezone.utc)
class Base(DeclarativeBase): pass
class JobStatus(str, Enum):
    NEW="NEW"; NOTIFIED="NOTIFIED"; REVIEWING="REVIEWING"; APPLIED="APPLIED"; SAVED="SAVED"; INTERVIEW="INTERVIEW"; ASSESSMENT="ASSESSMENT"; REJECTED="REJECTED"; OFFER="OFFER"; IGNORED="IGNORED"; EXPIRED="EXPIRED"
class Job(Base):
    __tablename__="jobs"; __table_args__=(UniqueConstraint("fingerprint", name="uq_job_fingerprint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Broad company/title/location bucket. Unlike ``fingerprint``, it is not
    # unique: two genuinely distinct vacancies can share a title and location.
    identity_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    company: Mapped[str] = mapped_column(String(500), index=True)
    location: Mapped[str] = mapped_column(String(500), default="")
    work_setup: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    application_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    salary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date_posted: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_discovered: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Provider posting time, first discovery, and latest confirmation stay
    # distinct so a listing rediscovered today never becomes "posted today".
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    employment_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(80), nullable=True)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    match_reasons: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default=JobStatus.NEW.value, index=True)
    notification_state: Mapped[str] = mapped_column(String(24), default="PENDING")
class SourceRun(Base):
    __tablename__="source_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True); source: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now); completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered: Mapped[int] = mapped_column(Integer, default=0); new_jobs: Mapped[int] = mapped_column(Integer, default=0); filtered: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(default=False); error: Mapped[str | None] = mapped_column(Text, nullable=True)
class SourceHealth(Base):
    __tablename__='source_health'
    source: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_count: Mapped[int] = mapped_column(Integer, default=0)
    last_raw_jobs: Mapped[int] = mapped_column(Integer, default=0)
    last_normalized_jobs: Mapped[int] = mapped_column(Integer, default=0)
    last_accepted_jobs: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='unknown')
    baseline_initialized: Mapped[bool] = mapped_column(default=False)
class AppState(Base):
    __tablename__='app_state'
    key: Mapped[str] = mapped_column(String(100),primary_key=True)
    # Scheduler snapshots now include real bulk-scan counters and can exceed
    # the legacy 500-character limit. Existing deployments are upgraded by
    # Repository.create_schema().
    value: Mapped[str] = mapped_column(Text,default='')
class AppSetting(Base):
    """Non-secret runtime configuration loaded after database startup."""
    __tablename__='app_settings'
    key: Mapped[str] = mapped_column(String(100),primary_key=True)
    value: Mapped[str] = mapped_column(Text,default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=now,onupdate=now)
class JobEvent(Base):
    """Small immutable activity timeline for the private application tracker."""
    __tablename__='job_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
class SourceConnection(Base):
    """Encrypted human-authorized source session; never stores credentials."""
    __tablename__='source_connections'; __table_args__=(UniqueConstraint('discord_user_id','source', name='uq_source_connection_user'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default='AUTH REQUIRED', index=True)
    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
class SourceConnectionRequest(Base):
    __tablename__='source_connection_requests'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default='PENDING')
class ResumeProfile(Base):
    __tablename__='resume_profile'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100), default='application/pdf')
    file_data: Mapped[bytes] = mapped_column(LargeBinary)
    extracted_text: Mapped[str] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
