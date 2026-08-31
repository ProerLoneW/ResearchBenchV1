"""阅读统计与督促 Dashboard 数据聚合。"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

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


# ===========================================================================
# 新版科研驾驶舱 Dashboard：全部指标仅读取论文现有字段计算，
# 不修改 IMA 写库逻辑（主链路保持不变）。缺失数据（阅读时长 / 笔记数 /
# radar 历史发现量）以 None / "—" 诚实降级，不伪造数字。
# ===========================================================================
# 领域 / 标签别名归一化：把 "VLA / WAM"、"vla"、"wam"、"World Model" 等
# 大小写 / 缩写 / 组合标签合并为统一展示名（仅影响看板统计，不改存储）。
_FIELD_ALIASES = {
    "vla": "VLA", "vision language action": "VLA", "vision-language-action": "VLA",
    "wam": "WAM", "world action model": "WAM",
    "world model": "World Model", "world models": "World Model",
    "world modeling": "World Model", "world model(wm)": "World Model",
    "embodied ai": "Embodied AI", "embodied": "Embodied AI", "embodied intelligence": "Embodied AI",
    "humanoid": "Humanoid", "humanoid robot": "Humanoid", "humanoid robots": "Humanoid",
    "agent": "Agent", "llm agent": "Agent", "ai agent": "Agent",
    "multimodal": "Multimodal", "multi-modal": "Multimodal", "multi modal": "Multimodal",
    "rag": "RAG", "retrieval-augmented generation": "RAG",
    "robot learning": "Robot Learning", "robotic learning": "Robot Learning",
    "rl": "RL", "reinforcement learning": "RL",
    "llm": "LLM", "large language model": "LLM",
    "diffusion": "Diffusion", "diffusion model": "Diffusion",
    "sim2real": "Sim2Real", "sim-to-real": "Sim2Real",
    "grasp": "Grasping", "grasping": "Grasping",
    "navigation": "Navigation", "vlmn": "VLM", "vlm": "VLM",
    "vision language model": "VLM", "vision-language model": "VLM",
}


def _norm(text: str) -> str:
    if not text:
        return "未分类"
    t = text.strip()
    key = t.lower()
    if key in _FIELD_ALIASES:
        return _FIELD_ALIASES[key]
    # 折叠常见分隔符后再匹配（避免 "world model" vs "world-model" 分裂）
    key2 = re.sub(r"[\s/_\-]+", " ", key).strip()
    if key2 in _FIELD_ALIASES:
        return _FIELD_ALIASES[key2]
    return t


def _split_field(raw: str):
    """把 "VLA / WAM" 这类组合标签拆成多段分别归一化。"""
    if not raw or not raw.strip():
        return ["未分类"]
    return [seg.strip() for seg in re.split(r"\s*/\s*", raw) if seg.strip()]


def _paper_tags(p) -> list:
    """抽取一篇论文的研究领域标签（归一化、去噪）。

    真相来源是 tags（论文的 field 在本库数据里几乎都是「未分类」，
    领域信息实际写在 tags 中）。tags 可能是字符串也可能是列表，
    需要统一拆成单标签再做别名归一化；过滤掉单字符噪声。
    """
    raw = p.get("tags")
    if isinstance(raw, str):
        parts = re.split(r"[\s,，、;；]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = []
        for t in raw:
            if isinstance(t, str):
                parts.extend(re.split(r"[\s,，、;；]+", t))
            else:
                parts.append(str(t))
    else:
        return []
    out = []
    for seg in parts:
        seg = seg.strip()
        if not seg:
            continue
        for sub in _split_field(seg):        # 处理 "VLA/WAM" 这类
            nf = _norm(sub)
            if nf and len(nf) >= 2:           # 过滤单字符噪声
                out.append(nf)
    return out


def _pd(v):
    """兼容 datetime 或 ISO 字符串。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None


class DashStatus(BaseModel):
    read: int
    reading: int
    unread: int
    total: int


class DashPipeline(BaseModel):
    radar_added: int
    reading: int
    read: int
    notes: Optional[int] = None


class DashBacklog(BaseModel):
    unread: int
    avg_wait_days: float
    over30: int
    week_new: int
    week_read: int


class DashSuggest(BaseModel):
    id: int
    title: str
    field: str
    reason: str


class DashRadar(BaseModel):
    added_week: int
    added_total: int
    top_keywords: list  # [{tag, count}]


class DashboardOut(BaseModel):
    # —— 顶层原始指标（前端据此拼 KPI 卡）——
    week_read: int
    prev_week_read: int
    week_new: int
    prev_week_new: int
    unread_count: int
    radar_added: int
    radar_added_week: int
    weekly_goal: int
    weekly_progress: int
    weekly_forecast: str
    # —— 各模块 ——
    trend: list            # [{date, count, minutes, notes}]
    trend_has_time: bool
    trend_has_notes: bool
    fields: list           # [{field, count}]（已归一化，降序）
    status: DashStatus
    pipeline: DashPipeline
    backlog: DashBacklog
    suggest: Optional[DashSuggest] = None
    radar: DashRadar
    heatmap: list          # [{date, score, dow}] 最近 90 天
    shift: list            # [{field, last30, prev30, pct}]
    has_notes: bool = False
    stale: bool = False
    warning: str = ""
    generated_at: str


@router.get("/dashboard", response_model=DashboardOut)
def get_dashboard(db: Session = Depends(get_db)):
    now = datetime.now()
    today = now.date()
    week_start = _start_of_week(now)                       # 周一 00:00
    prev_week_start = week_start - timedelta(days=7)
    last30_start = today - timedelta(days=30)
    prev30_start = today - timedelta(days=60)
    days_left_week = 7 - today.weekday()                  # 含今天剩余天数

    papers = ima_store.papers.all()

    read_per_day = Counter()
    read_dates = set()
    domain_counter = Counter()      # 研究领域分布（取自归一化后的 tags）
    status_counter = Counter()
    radar_added = 0
    radar_added_week = 0
    week_read = prev_week_read = 0
    week_new = prev_week_new = 0
    activity = defaultdict(int)
    unread_created = []
    # 近 30 天已读论文的领域（用于"今日建议"相关性打分）
    active_fields = Counter()

    for p in papers:
        st = (p.get("reading_status") or "unread")
        status_counter[st] += 1
        tags = _paper_tags(p)                 # 归一化后的研究领域标签（主信号）
        for t in tags:
            domain_counter[t] += 1
        src = (p.get("source") or "manual")
        if src == "radar":
            radar_added += 1
        created = _pd(p.get("created_at"))
        read = _pd(p.get("read_at"))
        fav = _pd(p.get("favorited_at"))

        if created:
            cd = created.date()
            if cd >= week_start.date():
                week_new += 1
                if src == "radar":
                    radar_added_week += 1
            if prev_week_start.date() <= cd < week_start.date():
                prev_week_new += 1
            activity[cd] += 1  # 收录
        if read:
            rd = read.date()
            read_dates.add(rd)
            read_per_day[rd] += 1
            if rd >= week_start.date():
                week_read += 1
            if prev_week_start.date() <= rd < week_start.date():
                prev_week_read += 1
            activity[rd] += 3  # 完成阅读
            if rd >= last30_start:
                for t in tags:
                    active_fields[t] += 1
        if fav:
            activity[fav.date()] += 2  # 收藏
        if st == "unread" and created:
            unread_created.append(created.date())

    # —— 趋势（近 90 天，按阅读篇数；时长/笔记暂无数据，前端按 7/30/90 切片）——
    trend = []
    for i in range(89, -1, -1):
        d = today - timedelta(days=i)
        trend.append({
            "date": d.strftime("%m-%d"),
            "count": read_per_day.get(d, 0),
            "minutes": None,
            "notes": None,
        })

    # —— 论文状态 ——
    status = DashStatus(
        read=status_counter.get("read", 0),
        reading=status_counter.get("reading", 0),
        unread=status_counter.get("unread", 0),
        total=sum(status_counter.values()),
    )

    # —— Research Pipeline（笔记未持久化 → None）——
    pipeline = DashPipeline(
        radar_added=radar_added,
        reading=status.reading,
        read=status.read,
        notes=None,
    )

    # —— 待读积压 ——
    wait_days = [(today - c).days for c in unread_created]
    backlog = DashBacklog(
        unread=status.unread,
        avg_wait_days=round(sum(wait_days) / len(wait_days), 1) if wait_days else 0.0,
        over30=sum(1 for w in wait_days if w > 30),
        week_new=week_new,
        week_read=week_read,
    )

    # —— 今日建议 / 下一篇读什么 ——
    suggest = _pick_next_read(papers, active_fields, today)

    # —— Radar 概览（历史发现量未持久化，以"收录自 Radar"代理）——
    radar = DashRadar(
        added_week=radar_added_week,
        added_total=radar_added,
        top_keywords=[{"tag": t, "count": c} for t, c in domain_counter.most_common(10)],
    )

    # —— 90 天活跃度热力图 ——
    heatmap = []
    for i in range(89, -1, -1):
        d = today - timedelta(days=i)
        heatmap.append({
            "date": d.strftime("%Y-%m-%d"),
            "score": activity.get(d, 0),
            "dow": d.weekday(),
        })

    # —— 研究方向变化（近 30 天 vs 前 30 天，按已读论文领域）——
    shift = _interest_shift(papers, last30_start, prev30_start)

    # —— 目标预测 ——
    prefs = db.query(UserPrefs).filter(UserPrefs.id == 1).first()
    weekly_goal = prefs.weekly_goal if prefs else 5
    weekly_progress = week_read
    weekly_forecast = _forecast(weekly_progress, weekly_goal, days_left_week)

    # —— 领域分布（归一化，降序）——
    fields_sorted = [
        {"field": f, "count": c} for f, c in domain_counter.most_common()
    ]

    st = ima_store.papers.state
    stale = bool(getattr(st, "stale", False))
    warning = (
        "IMA 知识库暂时不可用，当前展示的是本地缓存快照，数据可能不是最新。"
        if stale else ""
    )

    return DashboardOut(
        week_read=week_read,
        prev_week_read=prev_week_read,
        week_new=week_new,
        prev_week_new=prev_week_new,
        unread_count=status.unread,
        radar_added=radar_added,
        radar_added_week=radar_added_week,
        weekly_goal=weekly_goal,
        weekly_progress=weekly_progress,
        weekly_forecast=weekly_forecast,
        trend=trend,
        trend_has_time=False,
        trend_has_notes=False,
        fields=fields_sorted,
        status=status,
        pipeline=pipeline,
        backlog=backlog,
        suggest=suggest,
        radar=radar,
        heatmap=heatmap,
        shift=shift,
        has_notes=False,
        stale=stale,
        warning=warning,
        generated_at=now.strftime("%Y-%m-%d %H:%M"),
    )


def _pick_next_read(papers, active_fields: Counter, today):
    """从「未读」里挑一篇最该读的：未读基础分 + 收藏久 + 与近期方向相关 + 来自 Radar。"""
    if not active_fields:
        top_active = set()
    else:
        top_active = {f for f, _ in active_fields.most_common(3)}
    best = None
    best_score = -1
    for p in papers:
        if (p.get("reading_status") or "unread") != "unread":
            continue
        score = 3
        reasons = []
        created = _pd(p.get("created_at"))
        if created:
            age = (today - created.date()).days
            if age >= 7:
                score += 2
                reasons.append(f"收藏 {age} 天")
        ptags = _paper_tags(p)
        match = top_active & set(ptags)
        if match:
            score += 2
            reasons.append("与近期方向相关")
        if (p.get("source") or "") == "radar":
            score += 1
            reasons.append("来自 Radar")
        if score > best_score:
            best_score = score
            best = {
                "id": p.get("id"),
                "title": p.get("title") or "未命名",
                "field": (ptags[0] if ptags else "未分类"),
                "reason": "、".join(reasons) if reasons else "尚未开始阅读",
            }
    return DashSuggest(**best) if best else None


def _interest_shift(papers, last30_start, prev30_start):
    last30 = Counter()
    prev30 = Counter()
    for p in papers:
        read = _pd(p.get("read_at"))
        if not read:
            continue
        rd = read.date()
        for t in _paper_tags(p):
            if last30_start <= rd:
                last30[t] += 1
            elif prev30_start <= rd < last30_start:
                prev30[t] += 1
    rows = []
    for f in set(list(last30) + list(prev30)):
        l = last30.get(f, 0)
        pv = prev30.get(f, 0)
        pct = None
        if pv > 0:
            pct = round((l - pv) / pv * 100, 0)
        elif l > 0:
            pct = None  # 新出现
        rows.append({"field": f, "last30": l, "prev30": pv, "pct": pct})
    rows.sort(key=lambda r: r["last30"], reverse=True)
    return rows[:10]


def _forecast(progress: int, goal: int, days_left: int) -> str:
    if goal <= 0:
        return "未设目标"
    if progress >= goal:
        return "已完成 🎉"
    if days_left <= 0:
        return "本周已结束"
    remain = goal - progress
    need = remain / days_left
    if need <= 1:
        return "可以完成"
    if need <= 2:
        return "需要加快"
    return "较难完成"
