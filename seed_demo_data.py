"""
构造演示数据：在 ResearchBench 的 SQLite 中写入若干跨天的论文阅读记录与 AI 资讯，
用于直观查看 Dashboard 趋势图、领域分布、资讯库等可视化效果。

运行：在 ResearchBench 目录下用 venv python 执行
  python seed_demo_data.py

本脚本为幂等友好：先将已有的「演示数据」(title 以 [demo] 标记) 清空再写入，
不影响你手动加入的真实论文（除非它们也恰好以 [demo] 开头，一般不会）。
"""
import random
from datetime import datetime, timedelta

from app.db import SessionLocal
from app.models import Field, Paper, NewsItem

random.seed(20260823)

FIELDS = ["VLA / WAM", "World Model", "Multimodal", "Agent", "RAG", "具身智能"]

# 每个领域准备一些拟真论文标题
PAPER_TITLES = {
    "VLA / WAM": [
        "OpenVLA-OCR: 面向真实机器人操作的视觉-语言-动作模型",
        "WAM: A Workflow-Action Model for Long-Horizon Manipulation",
        "Scaling Vision-Language-Action Models with Synthetic Teleop Data",
        "Grounded VLA: 将自然语言指令对齐到关节级动作的策略",
        "RT-X-Lite: 轻量化视觉-语言-动作模型在边缘设备上的部署",
        "Cross-Embodiment VLA Pretraining from Human Videos",
    ],
    "World Model": [
        "Dreamer-V4 on Real Robots: 从像素到控制的潜空间世界模型",
        "Genie-2 Revisited: 可交互生成式世界模型的时序一致性分析",
        "TD-MPC-2 for Quadruped Locomotion in the Wild",
        "Learning Predictive World Models from Passive Camera Feeds",
        "JEPA-2: 自监督世界模型在视频表征上的迁移",
        "Latent Consistency World Models for Sample-Efficient RL",
    ],
    "Multimodal": [
        "MM-LLM-Align: 多模态大模型跨模态对齐的残差方法",
        "Video-LLaVA-Next: 长视频时序推理的高效架构",
        "AnyModal: 统一任意模态输入的路由器设计",
        "视觉-语言检索中的难负样本挖掘策略",
        "Multimodal Chain-of-Thought for Scientific Diagrams",
        "OCR-Free Document Understanding with High-Res Encoders",
    ],
    "Agent": [
        "Toolformer-Style Agent with Self-Verification Loops",
        "Multi-Agent Debate for Robust Code Generation",
        "ReAct-Pro: 带规划反思的自主智能体框架",
        "BrowserGym: 网页智能体的端到端基准",
        "Memory-Augmented Agents for Long-Context Tasks",
        "Agent Workflows as Composable Directed Graphs",
    ],
    "RAG": [
        "HyDE-RAG: 假设性文档嵌入的检索增强生成",
        "GraphRAG: 基于知识图谱的全局摘要检索",
        "Self-RAG with Adaptive Retrieval Triggering",
        "长文档分块策略对检索召回的影响研究",
        "RAG-Fusion: 多查询融合的召回重排方法",
        "Agentic RAG: 检索即工具调用的智能体范式",
    ],
    "具身智能": [
        "Humanoid Locomotion Transfer from Simulation to Reality",
        "VLA 在工厂巡检机器人上的落地实践",
        "Quadruped Whole-Body Control via Diffusion Policies",
        "具身智能的数据飞轮：从遥操作到自主抓取",
        "Sim-to-Real Gap in Tactile Sensing for Manipulation",
        "Robot Foundation Models Pretrained on Heterogeneous Datasets",
    ],
}

NEWS_SOURCES = ["量子位", "36氪", "InfoQ", "机器之心", "新智元", "The Verge"]
NEWS_TEMPLATES = [
    "{}发布新一代具身智能模型，瞄准工业落地",
    "融资速递：{}完成数亿元 AI 机器人融资",
    "深度：{}如何把大模型塞进边缘机器人",
    "{}开源世界模型代码库，社区反响热烈",
    "行业观察：多模态 Agent 正在重塑软件工程",
    "{}在世界机器人大会展示实景作业方案",
    "RAG 进入生产环境：企业知识库落地新范式",
    "{}发布 Agent 编排平台，支持可视化工作流",
    "人物 | {}团队谈机器人学的下一个十年",
    "评测：主流 VLA 模型在真实抓取任务上的对比",
]

NOW = datetime.now()
TODAY = NOW.date()


def make_papers(s):
    # 创建 / 获取领域
    field_ids = {}
    for name in FIELDS:
        f = s.query(Field).filter(Field.name == name).first()
        if not f:
            f = Field(name=name)
            s.add(f); s.flush()
        field_ids[name] = f.id

    # 清空旧演示数据
    old = s.query(Paper).filter(Paper.title.like("[demo]%")).all()
    for p in old:
        s.delete(p)
    s.flush()

    papers = []
    pid = 1000
    # 过去 14 天，构造有起伏的阅读节奏（周末略少，制造连续/中断的真实感）
    for day_back in range(13, -1, -1):
        d = TODAY - timedelta(days=day_back)
        weekday = d.weekday()
        # 每天阅读 0~6 篇，周末偏少
        base = 5 if weekday < 5 else 2
        n_today = max(0, min(6, base + random.randint(-2, 2)))
        for _ in range(n_today):
            field_name = random.choice(FIELDS)
            title = random.choice(PAPER_TITLES[field_name])
            # 同一天不同篇，标题加序号避免完全重复
            ts = datetime(d.year, d.month, d.day,
                          random.randint(8, 22), random.randint(0, 59))
            status = random.choices(
                ["read", "reading", "unread"],
                weights=[0.6, 0.25, 0.15])[0]
            read_at = ts if status == "read" else None
            arxiv_id = f"2{d:02d}{ts.month:02d}.{pid:05d}"
            pid += 1
            p = Paper(
                title=f"[demo]{title}",
                abstract=f"这是一条演示论文摘要，属于 {field_name} 领域。"
                         f"用于展示 ResearchBench 的跨天阅读趋势与领域分布可视化。",
                field_id=field_ids[field_name],
                tags=field_name,
                original_url=f"https://arxiv.org/abs/{arxiv_id}",
                github_url=random.choice(["", "https://github.com/example/repo"]),
                reading_status=status,
                arxiv_id=arxiv_id,
                source="radar",
                read_at=read_at,
                created_at=ts - timedelta(hours=random.randint(1, 48)),
            )
            papers.append(p)
    s.add_all(papers)
    s.flush()
    print(f"  写入论文 {len(papers)} 篇（分布在过去 14 天）")


def make_news(s):
    old = s.query(NewsItem).filter(NewsItem.title.like("[demo]%")).all()
    for n in old:
        s.delete(n)
    s.flush()

    items = []
    nid = 5000
    # 过去 10 天，每天 1~3 条资讯，来源混合
    for day_back in range(9, -1, -1):
        d = TODAY - timedelta(days=day_back)
        n_today = random.randint(1, 3)
        for _ in range(n_today):
            src = random.choice(NEWS_SOURCES)
            tmpl = random.choice(NEWS_TEMPLATES)
            title = tmpl.format(src)
            ts = datetime(d.year, d.month, d.day,
                          random.randint(9, 21), random.randint(0, 59))
            items.append(NewsItem(
                title=f"[demo]{title}",
                url=f"https://example.com/news/{nid}",
                source=src,
                published=ts.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                summary=f"{src} 带来的 AI / 机器人领域最新动态（演示数据）。",
                field=random.choice(FIELDS),
                created_at=ts,
            ))
            nid += 1
    s.add_all(items)
    s.flush()
    print(f"  写入资讯 {len(items)} 条（分布过去 10 天）")


def main():
    s = SessionLocal()
    try:
        print("构造演示数据 ...")
        make_papers(s)
        make_news(s)
        s.commit()
        print("完成。打开 Dashboard / 资讯库即可看到可视化效果。")
    finally:
        s.close()


if __name__ == "__main__":
    main()
