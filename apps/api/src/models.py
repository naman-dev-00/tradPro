import datetime
import uuid
from sqlalchemy import Column, String, DateTime, JSON, Boolean, CheckConstraint, UniqueConstraint, ForeignKey, text
from src.database import Base, UTCDateTime

LEGACY_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000000"

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(50), nullable=False)
    normalized_username = Column(String(50), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False)
    normalized_email = Column(String(255), nullable=False, unique=True, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="VIEWER")
    is_active = Column(Boolean, nullable=False, default=True, server_default=text('true'), index=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("role IN ('VIEWER', 'EDITOR', 'ADMIN')", name="ck_users_role"),
        CheckConstraint("length(username) >= 3 AND length(username) <= 50", name="ck_users_username_len"),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_hash = Column(String(64), nullable=False, unique=True, index=True)
    csrf_hash = Column(String(64), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    last_accessed_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    idle_expires_at = Column(UTCDateTime, nullable=False, index=True)
    absolute_expires_at = Column(UTCDateTime, nullable=False, index=True)
    is_revoked = Column(Boolean, nullable=False, default=False, server_default=text('false'), index=True)
    revoked_at = Column(UTCDateTime, nullable=True)


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)
    timeframe = Column(String(50), nullable=False)
    candidate_selection_mode = Column(String(50), nullable=False, default="FIRST_ELIGIBLE")
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    @property
    def action(self):
        return self.payload.get("action")

    @property
    def global_conditions(self):
        return self.payload.get("global_conditions")

    @property
    def candidate_conditions(self):
        return self.payload.get("candidate_conditions")


class InspectionRun(Base):
    __tablename__ = "inspection_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    strategy_id = Column(String(36), index=True, nullable=True)
    strategy_version_snapshot = Column(String(100), nullable=True)
    strategy_definition_snapshot = Column(JSON(none_as_null=True), nullable=True)
    run_type = Column(String(50), index=True, nullable=False)
    reference_dataset_id = Column(String(255), nullable=True)
    subject_dataset_ids = Column(JSON, nullable=False)
    requested_start_timestamp = Column(UTCDateTime, nullable=True)
    requested_end_timestamp = Column(UTCDateTime, nullable=True)
    requested_evaluation_timestamp = Column(UTCDateTime, nullable=True)
    timeframe = Column(String(50), nullable=False)
    engine_version = Column(String(50), nullable=False, default="1.0.0")
    manifest_version = Column(String(50), nullable=False, default="1.0.0")
    created_at = Column(UTCDateTime, index=True, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    completed_at = Column(UTCDateTime, nullable=True)
    status = Column(String(50), index=True, nullable=False)
    failure_summary = Column(String(2048), nullable=True)
    result_summary = Column(String(2048), nullable=True)
    result_payload = Column(JSON(none_as_null=True), nullable=True)
    synthetic_data_confirmed = Column(Boolean, nullable=False, default=True, server_default=text('true'))
    request_fingerprint = Column(String(64), index=True, nullable=True)
    completed_fingerprint = Column(String(64), index=True, nullable=True)
    manifest_checksums_snapshot = Column(JSON(none_as_null=True), nullable=True)

    __table_args__ = (
        CheckConstraint("run_type IN ('SINGLE_SERIES', 'MULTI_SERIES', 'HISTORICAL_REPLAY')", name="ck_inspection_runs_run_type"),
        CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_inspection_runs_status"),
        CheckConstraint("synthetic_data_confirmed IS TRUE", name="ck_inspection_runs_synthetic_confirmed"),
        CheckConstraint(
            "status != 'COMPLETED' OR ("
            "completed_at IS NOT NULL AND "
            "strategy_definition_snapshot IS NOT NULL AND "
            "reference_dataset_id IS NOT NULL AND "
            "subject_dataset_ids IS NOT NULL AND "
            "requested_start_timestamp IS NOT NULL AND "
            "requested_end_timestamp IS NOT NULL AND "
            "result_payload IS NOT NULL AND "
            "manifest_checksums_snapshot IS NOT NULL AND "
            "failure_summary IS NULL"
            ")",
            name="ck_inspection_runs_completed_fields"
        ),
        CheckConstraint(
            "status != 'FAILED' OR ("
            "failure_summary IS NOT NULL AND "
            "result_payload IS NULL"
            ")",
            name="ck_inspection_runs_failed_fields"
        ),
        UniqueConstraint("owner_id", "completed_fingerprint", name="uq_inspection_runs_owner_completed_fingerprint"),
    )
