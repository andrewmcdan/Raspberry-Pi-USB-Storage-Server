import time
import uuid
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = 'devices'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    serial: Mapped[str] = mapped_column(String(200), unique=True)
    credential: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(200), default='')
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default='')
    window: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen: Mapped[float] = mapped_column(Float, default=0)
    telemetry: Mapped[dict] = mapped_column(JSON, default=dict)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    local: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    draft: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Group(Base):
    __tablename__ = 'groups'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    devices: Mapped[list] = mapped_column(JSON, default=list)


class Collection(Base):
    __tablename__ = 'collections'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    draft: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Enrollment(Base):
    __tablename__ = 'enrollment'
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Deployment(Base):
    __tablename__ = 'deployments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch: Mapped[str] = mapped_column(String(36), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.id'), index=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    manifest: Mapped[list | None] = mapped_column(JSON, nullable=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default='queued')
    policy: Mapped[str] = mapped_column(String(32), default='auto')
    due: Mapped[float] = mapped_column(Float, default=0)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    resume_local: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    payload: Mapped[dict] = mapped_column(JSON)
    submitted: Mapped[bool] = mapped_column(Boolean, default=False)


class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSON)
