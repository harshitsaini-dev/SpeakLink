"""SpeakLink database models."""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Text, Index
)
from sqlalchemy.orm import relationship
from db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Store(Base):
    __tablename__ = "stores"
    id = Column(Integer, primary_key=True)
    store_code = Column(String(50), unique=True, nullable=False, index=True)
    store_name = Column(String(200), nullable=False)
    city = Column(String(100), nullable=False, index=True)
    region = Column(String(100), nullable=False, index=True)
    is_online_store = Column(Boolean, default=False, nullable=False)
    receiver_token = Column(String(64), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    status = Column(String(20), default="offline", nullable=False)  # online|offline|playing|error
    last_seen = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class HQUser(Base):
    __tablename__ = "hq_users"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="admin", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class LoginSecurityState(Base):
    """Consecutive failed sign-ins for one real account.

    Deliberately a separate table rather than columns on ``hq_users``. Adding
    columns would need an ALTER TABLE against the table that holds every
    password hash; a new table is created by ``create_all`` on an existing
    database without touching a single existing row.

    A row exists only for a username that really exists. An unknown name is
    handled by the in-process limiter and never reaches this table, so an
    attacker cannot turn invented usernames into unbounded rows - and a lock can
    never be reported for an account that does not exist, which would confirm
    that it does.

    Times are epoch seconds rather than DateTime so a lock survives a restart
    and compares without any timezone ambiguity. Nothing here holds a
    credential.
    """

    __tablename__ = "login_security_state"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    failed_count = Column(Integer, default=0, nullable=False)
    locked_until_epoch = Column(Float, nullable=True)
    last_failed_epoch = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class BroadcastSession(Base):
    __tablename__ = "broadcast_sessions"
    id = Column(Integer, primary_key=True)
    campaign_name = Column(String(255), nullable=False)
    started_by = Column(Integer, ForeignKey("hq_users.id"), nullable=False)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    status = Column(String(30), default="pending", nullable=False, index=True)
    # pending | live | ended | emergency_stopped | failed
    target_mode = Column(String(30), nullable=False)  # all|selected|region|city|online_only
    selected_store_count = Column(Integer, default=0)
    online_store_count = Column(Integer, default=0)
    offline_store_count = Column(Integer, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    targets = relationship("BroadcastTarget", back_populates="session", cascade="all, delete-orphan")
    starter = relationship("HQUser")


class BroadcastTarget(Base):
    __tablename__ = "broadcast_targets"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("broadcast_sessions.id"), nullable=False, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    play_status = Column(String(30), default="pending", nullable=False)  # pending|playing|stopped|failed
    command_sent_at = Column(DateTime, nullable=True)
    started_playing_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    session = relationship("BroadcastSession", back_populates="targets")
    store = relationship("Store")


class ReceiverEvent(Base):
    __tablename__ = "receiver_events"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    event_type = Column(String(50), nullable=False)  # connected|disconnected|heartbeat|play_ack|stop_ack|error
    event_time = Column(DateTime, default=utcnow, nullable=False)
    details = Column(Text, nullable=True)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id = Column(Integer, primary_key=True)
    level = Column(String(20), nullable=False, index=True)  # info|warn|error
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


Index("ix_receiver_events_store_time", ReceiverEvent.store_id, ReceiverEvent.event_time.desc())
