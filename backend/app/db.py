import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, event, text)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.types import TypeDecorator

from . import config

_is_sqlite = config.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JSONText(TypeDecorator):
    """JSON stored as text; works the same on SQLite and PostgreSQL."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(120), default="")
    password_hash = Column(String(255), nullable=False)
    settings = Column(JSONText, default=dict)
    created_at = Column(DateTime, default=now)


class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    name = Column(String(200), nullable=False)
    streamer = Column(String(120), default="")
    whop_url = Column(String(500), default="")
    instructions = Column(Text, default="")
    payout_per_1k = Column(Float, default=0.0)
    max_budget = Column(Float)
    max_payout_per_clip = Column(Float)
    min_duration = Column(Float, default=15)
    max_duration = Column(Float, default=60)
    required_hashtags = Column(JSONText, default=list)
    required_cta = Column(String(300), default="")
    approved_platforms = Column(JSONText, default=list)
    prohibited = Column(JSONText, default=list)          # terms that must not appear
    submission_instructions = Column(Text, default="")
    deadline = Column(DateTime)
    status = Column(String(20), default="active")        # lead | active | paused | ended
    require_watermark = Column(Boolean, default=False)
    watermark_text = Column(String(120), default="")
    require_attribution = Column(Boolean, default=True)
    notes = Column(Text, default="")
    preset = Column(JSONText, default=dict)              # layout / caption defaults for this campaign
    # rules that real campaign briefs carry (added in schema v2)
    min_views = Column(Integer, default=0)               # a clip earns nothing below this many views
    min_tier1_pct = Column(Float)                        # required share of viewers in the campaign's listed countries
    submit_within_min = Column(Integer)                  # submit the link within N minutes of posting
    keep_live_days = Column(Integer)                     # clip must stay up this long
    cta_onscreen = Column(String(200), default="")       # text card shown at the end of every clip
    auto_hook = Column(Boolean, default=True)            # write an opening hook line automatically
    created_at = Column(DateTime, default=now)


class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    title = Column(String(300), default="")
    streamer = Column(String(120), default="")
    filename = Column(String(300), default="")
    path = Column(String(600), default="")
    size = Column(Integer, default=0)
    received = Column(Integer, default=0)
    status = Column(String(20), default="uploading")     # uploading|uploaded|analyzing|ready|failed
    authorized = Column(Boolean, default=False)          # user attested they may use this footage
    source_note = Column(String(500), default="")
    notes = Column(Text, default="")
    duration = Column(Float)
    width = Column(Integer)
    height = Column(Integer)
    fps = Column(Float)
    vcodec = Column(String(40))
    acodec = Column(String(40))
    facecam = Column(JSONText)                           # {"x","y","w","h"} normalised, or None
    facecam_source = Column(String(20), default="")      # auto | manual
    has_chat = Column(Boolean, default=False)
    transcribe_provider = Column(String(40), default="")
    semantic_provider = Column(String(40), default="")
    cost = Column(JSONText, default=dict)
    analysis_settings = Column(JSONText, default=dict)
    error = Column(Text, default="")
    created_at = Column(DateTime, default=now)


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    type = Column(String(30), nullable=False)            # analyze | render | import_url
    video_id = Column(Integer, ForeignKey("videos.id"))
    clip_id = Column(Integer)
    payload = Column(JSONText, default=dict)
    status = Column(String(20), default="queued", index=True)  # queued|running|done|failed|cancelled
    progress = Column(Float, default=0.0)
    stage = Column(String(120), default="")
    error = Column(Text, default="")
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=2)
    created_at = Column(DateTime, default=now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class Highlight(Base):
    __tablename__ = "highlights"
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("videos.id"), index=True, nullable=False)
    rank = Column(Integer, default=0)
    start = Column(Float, nullable=False)
    end = Column(Float, nullable=False)
    peak = Column(Float)
    title = Column(String(300), default="")
    title_options = Column(JSONText, default=list)
    description = Column(Text, default="")
    reason = Column(Text, default="")
    hook = Column(Text, default="")
    hashtags = Column(JSONText, default=list)
    viral_potential = Column(Float, default=0)           # 0-100
    engagement_score = Column(Float, default=0)          # 0-100
    confidence = Column(Float, default=0)                # 0-1
    suggested_duration = Column(Float, default=0)
    transcript = Column(Text, default="")
    category = Column(String(60), default="")
    flags = Column(JSONText, default=list)               # [{"code","severity","detail"}]
    signals = Column(JSONText, default=dict)
    scored_by = Column(String(40), default="heuristic")
    status = Column(String(20), default="suggested")     # suggested | clipped | dismissed


class Clip(Base):
    __tablename__ = "clips"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    video_id = Column(Integer, ForeignKey("videos.id"), index=True, nullable=False)
    highlight_id = Column(Integer)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    title = Column(String(300), default="")
    description = Column(Text, default="")
    hashtags = Column(JSONText, default=list)
    start = Column(Float, nullable=False)
    end = Column(Float, nullable=False)
    layout = Column(String(30), default="blurfit")
    layout_params = Column(JSONText, default=dict)
    caption_style = Column(String(30), default="pop")
    caption_params = Column(JSONText, default=dict)
    words = Column(JSONText, default=list)               # [{"s","e","w"}] absolute source times
    audio_gain_db = Column(Float, default=0.0)
    normalize_audio = Column(Boolean, default=True)
    watermark_text = Column(String(120), default="")
    watermark_pos = Column(String(20), default="top")
    quality = Column(String(20), default="standard")
    status = Column(String(20), default="draft")         # draft|rendering|rendered|approved|rejected
    flags = Column(JSONText, default=list)
    flags_cleared = Column(Boolean, default=False)       # human reviewed the content flags
    reject_reason = Column(String(300), default="")
    output_path = Column(String(600), default="")
    rendered_hash = Column(String(40), default="")
    out_meta = Column(JSONText, default=dict)
    compliance = Column(JSONText)
    category = Column(String(60), default="")            # moment type, used to learn what performs
    exported_at = Column(DateTime)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Submission(Base):
    __tablename__ = "submissions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    clip_id = Column(Integer, ForeignKey("clips.id"))
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    platform = Column(String(40), default="")
    url = Column(String(600), default="")
    views = Column(Integer, default=0)
    approved_views = Column(Integer, default=0)
    confirmed_earnings = Column(Float, default=0.0)
    payment_status = Column(String(20), default="pending")  # pending|approved|paid|rejected
    payment_date = Column(DateTime)
    submitted_at = Column(DateTime, default=now)
    notes = Column(Text, default="")
    tier1_pct = Column(Float)                            # audience share in required countries, from platform analytics


class Snapshot(Base):
    """Point-in-time view counts, written whenever a submission's numbers are updated."""
    __tablename__ = "snapshots"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    at = Column(DateTime, default=now)
    views = Column(Integer, default=0)
    approved_views = Column(Integer, default=0)
    estimated = Column(Float, default=0.0)


class Activity(Base):
    __tablename__ = "activity"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    kind = Column(String(40), default="")
    message = Column(String(500), default="")
    created_at = Column(DateTime, default=now)


# --- migrations -------------------------------------------------------------
# New tables are created by create_all. New columns on existing tables are listed here and added only when
# missing, so the same code upgrades an old database and starts a fresh one. Append; never edit old entries.
_TRUE = "1" if _is_sqlite else "TRUE"
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("campaigns", "min_views", "INTEGER DEFAULT 0"),
    ("campaigns", "min_tier1_pct", "FLOAT"),
    ("campaigns", "submit_within_min", "INTEGER"),
    ("campaigns", "keep_live_days", "INTEGER"),
    ("campaigns", "cta_onscreen", "VARCHAR(200) DEFAULT ''"),
    ("campaigns", "auto_hook", f"BOOLEAN DEFAULT {_TRUE}"),
    ("clips", "category", "VARCHAR(60) DEFAULT ''"),
    ("submissions", "tier1_pct", "FLOAT"),
]


def init_db() -> None:
    from sqlalchemy import inspect
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as c:
        for table, col, ddl in COLUMN_MIGRATIONS:
            if col not in {x["name"] for x in insp.get_columns(table)}:
                c.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_activity(db, user_id: int, kind: str, message: str) -> None:
    db.add(Activity(user_id=user_id, kind=kind, message=message[:500]))


def to_dict(obj, exclude: tuple = ()) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.name in exclude:
            continue
        v = getattr(obj, col.name)
        if isinstance(v, datetime):
            v = v.isoformat() + "Z"
        out[col.name] = v
    return out
