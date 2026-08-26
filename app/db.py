"""数据库初始化与 session 管理（SQLite + SQLAlchemy）。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import DB_PATH

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db():
    # 导入模型以注册表
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """为已有库补齐新增列（SQLite 不支持自动 ALTER，需手动补齐）。"""
    from sqlalchemy import text, inspect
    inspector = inspect(engine)
    existing = set()
    for table in inspector.get_table_names():
        existing.update(
            (table, c["name"]) for c in inspector.get_columns(table)
        )
    # Paper.tex_repo_path
    if ("papers", "tex_repo_path") not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE papers ADD COLUMN tex_repo_path TEXT DEFAULT ''"))
    # RadarConfig.lang
    if ("radar_configs", "lang") not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE radar_configs ADD COLUMN lang TEXT DEFAULT 'en'"))
    # RadarConfig.channel
    if ("radar_configs", "channel") not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE radar_configs ADD COLUMN channel TEXT DEFAULT 'google'"))



def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
