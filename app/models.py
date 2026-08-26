"""SQLAlchemy 数据模型。"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float
)
from sqlalchemy.sql import func

from .db import Base


class Field(Base):
    """研究领域 / 分类。"""
    __tablename__ = "fields"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Paper(Base):
    """论文卡片 / 详情。"""
    __tablename__ = "papers"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(400), nullable=False, index=True)
    abstract = Column(Text, default="")
    field_id = Column(Integer, ForeignKey("fields.id"), nullable=True, index=True)
    tags = Column(String(400), default="")          # 逗号分隔
    original_url = Column(String(600), default="")
    github_url = Column(String(600), default="")
    feishu_doc_url = Column(String(600), default="")
    summary = Column(Text, default="")              # 我的总结 / 笔记 / 心得
    tex_repo_path = Column(Text, default="")        # TeX 仓库路径（上传后存于 data/tex_repos/{id}）
    reading_status = Column(String(20), default="unread", index=True)  # unread/reading/read
    arxiv_id = Column(String(60), default="", index=True)
    source = Column(String(20), default="manual")   # manual / radar
    favorited_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class RadarConfig(Base):
    """Research Radar 检索模板（领域 + 关键词 + 说明）。"""
    __tablename__ = "radar_configs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    type = Column(String(10), default="paper", index=True)  # paper / news
    field = Column(String(120), default="")       # 领域名称
    keywords = Column(String(400), default="")    # 搜索关键词（逗号分隔）
    note = Column(String(300), default="")        # 补充说明
    enabled = Column(Boolean, default=True)        # 定时任务是否启用
    time_range_days = Column(Integer, default=2)   # 时间范围（天）
    lang = Column(String(10), default="en")        # 资讯语言：en / zh / auto
    channel = Column(String(10), default="google")  # 资讯渠道：google / cn / all
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ApiConfig(Base):
    """自定义 API 配置（仅一行，id=1）。API Key 加密存储。"""
    __tablename__ = "api_config"

    id = Column(Integer, primary_key=True, default=1)
    provider = Column(String(60), default="OpenAI-Compatible")
    base_url = Column(String(400), default="")
    api_key = Column(Text, default="")            # 加密后的密文
    model_name = Column(String(120), default="")
    other_params = Column(Text, default="{}")     # JSON 额外参数
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class UserPrefs(Base):
    """用户偏好（周目标等）。"""
    __tablename__ = "user_prefs"

    id = Column(Integer, primary_key=True, default=1)
    weekly_goal = Column(Integer, default=5)       # 每周阅读目标（篇）
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class NewsItem(Base):
    """资讯库条目（来自 Research Radar 的 AI 资讯）。只读沉淀，无修改功能。"""
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(400), nullable=False, index=True)
    url = Column(String(800), default="", index=True)
    source = Column(String(200), default="")        # 来源媒体 / 站点
    published = Column(String(80), default="")       # 发布时间（原始字符串）
    summary = Column(Text, default="")               # 简短摘要/备注
    field = Column(String(120), default="")          # 关联领域
    note = Column(String(300), default="")           # 用户备注
    created_at = Column(DateTime, server_default=func.now())
