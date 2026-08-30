"""阅读统计与督促 Dashboard 数据聚合。"""
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import UserPrefs
from ..services import ima_store

router = APIRouter(prefix="/api/stats", tags=["stats"])


class StatsOut(BaseModel):
    today_count: int
    week_count: int
    month_count: int
    new_count: int
    read_count: int
    unread_count: int
    reading_count: int
    streak_days: int
    weekly_goal: int
    weekly_progress: int
    field_distribution: list
    trend: list


def _start_of_week(now: datetime) -> datetime:
    # 周一为一周开始
    monday = now.date() - timedelta(days=now.weekday())
    return datetime(monday.year, monday.month, monday.day)


@router.get("", response_model=StatsOut)
def get_stats(db: Session = Depends(get_db)):
    now = datetime.now()
    today = now.date()
    week_start = _start_of_week(now)
    month_start = datetime(today.year, today.month, 1)
    week_ago = now - timedelta(days=7)

    # 论文数据来自 IMA 知识库（ima_store 带本地派生缓存）
    papers = ima_store.papers.all()

    read_dates = set()
    read_per_day = Counter()   # 每天实际阅读篇数（用于趋势图，避免同日多读被 set 去重成 1）
    field_counter = Counter()
    trend = []
    today_c = week_c = month_c = new_c = 0
    read_c = unread_c = reading_c = 0

    for p in papers:
        st = p.get("reading_status") or "unread"
        if st == "read":
            read_c += 1
        elif st == "reading":
            reading_c += 1
        else:
            unread_c += 1

        if p.get("read_at"):
            rd = p["read_at"].date()
            read_dates.add(rd)
            read_per_day[rd] += 1
            if rd == today:
                today_c += 1
            if rd >= week_start.date():
                week_c += 1
            if rd >= month_start.date():
                month_c += 1
            field_counter[p.get("field_name") or "未分类"] += 1

        if p.get("created_at") and p["created_at"] >= week_ago:
            new_c += 1

    # 连续阅读天数
    streak = 0
    d = today
    if d not in read_dates:
        d = today - timedelta(days=1)
    while d in read_dates:
        streak += 1
        d -= timedelta(days=1)

    # 近 14 天趋势（按实际阅读篇数，而非去重后的日期）
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        cnt = read_per_day.get(day, 0)
        trend.append({"date": day.strftime("%m-%d"), "count": cnt})

    prefs = db.query(UserPrefs).filter(UserPrefs.id == 1).first()
    weekly_goal = prefs.weekly_goal if prefs else 5

    field_distribution = [
        {"field": k, "count": v}
        for k, v in field_counter.most_common()
    ]

    return StatsOut(
        today_count=today_c,
        week_count=week_c,
        month_count=month_c,
        new_count=new_c,
        read_count=read_c,
        unread_count=unread_c,
        reading_count=reading_c,
        streak_days=streak,
        weekly_goal=weekly_goal,
        weekly_progress=week_c,
        field_distribution=field_distribution,
        trend=trend,
    )
