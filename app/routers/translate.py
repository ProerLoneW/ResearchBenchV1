"""
论文中译任务接口。

    POST /api/papers/{pid}/translate          建任务并自动开始翻译（后台线程）
    GET  /api/translate/tasks                 任务列表（含进度步骤，前端任务窗口轮询）
    POST /api/translate/tasks/{id}/complete   回填中文版 PDF（agent 手动模式用）
    POST /api/translate/tasks/{id}/fail       标记失败
    GET  /api/translate/ima_paths             论文在 IMA 里的资产路径（卡片展示用）

自动模式：设置页配置了大模型 API 后，建任务即自动开始执行——
下载源码 → 逐文件调大模型翻译 → xelatex 编译 → 上传 IMA + 回写路径；
未配置大模型时直接 400，前端也会把按钮置灰。
手动模式（agent 按 task.json + SKILL.md 执行）的 complete / fail 接口保留不变。
    GET  /api/translate/ima_paths             论文在 IMA 里的资产路径（卡片展示用）

权限：建任务 / 回填 / 标记失败都是写操作，admin_guard 默认拦截非 GET，
开启 ADMIN_PASSWORD 后需要 `X-Admin-Password` 头；两个 GET 是只读，一律放行，
agent 可以先 GET /api/translate/tasks 摸清待办。
失败一律收敛成中文可读原因，不向上抛裸异常。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..services import llm_translate
from ..services.translate_task import TaskError, complete_task, create_task, \
    fail_task, ima_paths, list_tasks

router = APIRouter(tags=["translate"])


class CompleteIn(BaseModel):
    zh_pdf_path: str


class FailIn(BaseModel):
    reason: str = ""


@router.post("/api/papers/{pid}/translate")
def translate_paper(pid: int):
    """
    为一篇论文建中译任务并**立即自动开始翻译**：

    拉取全新 arXiv LaTeX 源码 → 探测入口 → 写出 task.json →
    后台线程注入字体 / 调大模型逐文件翻译 / xelatex 编译 / 上传 IMA。

    前置条件：设置页已配置大模型 API，否则 400。
    同一论文已有正在执行的自动翻译时返回 409。
    """
    ready, why = llm_translate.llm_ready()
    if not ready:
        raise HTTPException(400, why + "；配置后即可使用「生成中文版」")

    if llm_translate.is_running_for_pid(int(pid)):
        raise HTTPException(409, "该论文已有正在执行的翻译任务，请等它完成后再试")

    try:
        # force_fresh：自动模式必须拿全新英文源码（上一轮已翻成中文的不能复用）
        task = create_task(pid, force_fresh=True)
    except TaskError as exc:
        code = exc.code
        if code == "not_found":
            raise HTTPException(404, exc.reason)
        if code == "upstream":
            raise HTTPException(502, exc.reason)
        # no_arxiv_id / no_source / no_entry —— 都是"这篇没法翻"，前端直接展示
        raise HTTPException(400, exc.reason)
    except Exception as exc:      # 兜底，绝不让裸异常冒出去
        raise HTTPException(502, f"创建翻译任务失败：{type(exc).__name__}: {exc}")

    started = llm_translate.start_task(task["task_id"])

    return {
        "ok": True,
        "task_id": task["task_id"],
        "auto": started,
        "task_dir": task["task_dir"],
        "task_json": task["task_json"],
        "entry_tex": task["entry_tex"],
        "source_dir": task["source_dir"],
        "output_pdf": task["output_pdf"],
        "status": "running" if started else task["status"],
    }


@router.get("/api/translate/tasks")
def get_tasks(status: Optional[str] = Query(
    None, description="按状态过滤：pending / done / failed")):
    """列出所有中译任务及状态（供 agent 查询待办）。"""
    try:
        tasks = list_tasks(status or "")
    except Exception as exc:
        raise HTTPException(502, f"读取任务列表失败：{type(exc).__name__}: {exc}")
    return {"total": len(tasks), "tasks": tasks}


@router.post("/api/translate/tasks/{task_id}/complete")
def complete(task_id: str, payload: CompleteIn):
    """
    agent 翻译 + 编译完成后回填。

    body: {"zh_pdf_path": "..."}（绝对路径，或相对任务目录 / 源码目录）。
    服务端负责校验产物、上传到 IMA，并把 ima_zh_pdf_path 写回论文的 front-matter。
    """
    try:
        rec = complete_task(task_id, payload.zh_pdf_path)
    except TaskError as exc:
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status, exc.reason)
    except Exception as exc:
        raise HTTPException(502, f"回填失败：{type(exc).__name__}: {exc}")
    return {"ok": True, "task_id": rec["task_id"], "pid": rec["pid"],
            "status": rec["status"], "ima_zh_pdf_path": rec["ima_zh_pdf_path"]}


@router.post("/api/translate/tasks/{task_id}/fail")
def fail(task_id: str, payload: Optional[FailIn] = None):
    """标记任务失败，reason 写进任务状态与 task.json。"""
    try:
        rec = fail_task(task_id, (payload.reason if payload else "") or "")
    except TaskError as exc:
        raise HTTPException(404, exc.reason)
    except Exception as exc:
        raise HTTPException(502, f"标记失败时出错：{type(exc).__name__}: {exc}")
    return {"ok": True, "task_id": rec["task_id"], "status": rec["status"],
            "reason": rec["reason"]}


@router.get("/api/translate/ima_paths")
def get_ima_paths():
    """
    各论文在 IMA 知识库里的资产路径（原文 PDF / TeX 源码 / 中文版 PDF）。

    IMA 的 Web 端没有可直接按路径打开的链接，所以前端只做**纯文本展示**，
    不伪造可点击 URL。字段为空表示该资产还没入库。
    """
    try:
        paths = ima_paths()
    except Exception as exc:
        raise HTTPException(502, f"读取 IMA 资产路径失败："
                                 f"{type(exc).__name__}: {exc}")
    return {"total": len(paths),
            "paths": {str(k): v for k, v in paths.items()}}
