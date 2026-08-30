"""
Radar 检索后台任务：分步骤进度上报 + 前端轮询取结果。

为什么要这套机制：
  检索是「多源、串行/并行混合、总耗时几秒到几十秒」的操作，原先
  POST /api/radar/run 一次请求黑盒等待，前端只能转圈圈。现在拆成：

    POST /api/radar/run_bg            → 立即返回 job_id，检索在后台跑
    GET  /api/radar/job/{job_id}      → 轮询：每个步骤的状态 + 最终结果

  每一步（读论文库标识 / 检索 arXiv / 抓 Google News / 抓国内 RSS /
  抓公众号 / 合并去重）都有 wait / running / ok / error 四态，
  前端按步骤渲染进度条，用户能看清"卡在哪一步"。

任务只存内存（检索结果不落盘）：完成态任务保留 30 分钟供前端取结果，
最多同时保留 30 个 job，超出按完成时间淘汰。服务重启后 job 丢失属于
预期行为——前端轮询 404 时提示重新检索即可。
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

_LOCK = threading.Lock()
JOBS: Dict[str, dict] = {}
_TTL_SECONDS = 1800          # 完成态任务保留 30 分钟
_MAX_JOBS = 30               # 内存里最多 30 个 job


def _prune_locked() -> None:
    """调用方需已持锁。清理过期 + 超量任务。"""
    now = time.time()
    stale = [jid for jid, j in JOBS.items()
             if j.get("_done_at") and now - j["_done_at"] > _TTL_SECONDS]
    for jid in stale:
        JOBS.pop(jid, None)
    while len(JOBS) > _MAX_JOBS:
        done = sorted((j for j in JOBS.values() if j.get("_done_at")),
                      key=lambda x: x["_done_at"])
        if not done:
            break                      # 全是 running，不再挤占
        JOBS.pop(done[0]["job_id"], None)


def create_job(meta: dict, steps: List[tuple]) -> dict:
    """steps: [(key, label), ...]，初始全部 wait 态。返回 job 快照。"""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "running",           # running / done / failed
        "created_at": time.time(),
        "meta": meta,
        "steps": [{"key": k, "label": lbl, "status": "wait", "detail": ""}
                  for k, lbl in steps],
        "result": None,
        "error": "",
    }
    with _LOCK:
        _prune_locked()
        JOBS[job_id] = job
    return copy.deepcopy(job)


def get_job(job_id: str) -> Optional[dict]:
    with _LOCK:
        job = JOBS.get(job_id)
        return copy.deepcopy(job) if job else None


def set_step(job_id: str, key: str, status: str, detail: str = "") -> None:
    """status: wait / running / ok / error。异常静默（进度上报不致命）。"""
    with _LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        for s in job["steps"]:
            if s["key"] == key:
                s["status"] = status
                if detail:
                    s["detail"] = detail
                return


def finish_job(job_id: str, result: Optional[dict] = None,
               error: str = "") -> None:
    with _LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = "failed" if error else "done"
        job["result"] = result
        job["error"] = error
        job["_done_at"] = time.time()


def make_progress_cb(job_id: str) -> Callable[..., None]:
    """给 news.fetch_news 用的 (stage, status, detail) 回调。"""
    def _cb(stage: str, status: str, detail: str = "") -> None:
        set_step(job_id, stage, status, detail)
    return _cb
