"""
全局配置：路径、密钥、飞书参数。
所有敏感/可变的配置集中在此，便于后台设置页面复用。
"""
import json
import os
from pathlib import Path

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "workbench.db"
SECRET_KEY_FILE = DATA_DIR / ".secret_key"

# ---------- 飞书参数（来自 test_feishu_final_version.py）----------
# 安全提示：不要在代码中硬编码 Secret。请通过环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET
# 提供，或在「设置-飞书」页覆盖（写入 data/feishu_config.json，已被 .gitignore 忽略）。
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_aa0ec67a13789bd8")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
FEISHU_REDIRECT_URI = "http://127.0.0.1:8765/callback"
FEISHU_TOKEN_FILE = str(DATA_DIR / "feishu_user_token.json")

# ---------- 加密（API Key 等敏感信息）----------
from cryptography.fernet import Fernet

def _load_or_create_secret_key() -> bytes:
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    SECRET_KEY_FILE.write_bytes(key)
    # 仅本人可读
    os.chmod(SECRET_KEY_FILE, 0o600)
    return key

FERNET_KEY = _load_or_create_secret_key()
_FERNET = Fernet(FERNET_KEY)

def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _FERNET.encrypt(plain.encode("utf-8")).decode("utf-8")

def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _FERNET.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""

# ---------- Radar 检索代理（解决内网/沙箱无法直连 arXiv / Google News）----------
# 优先级：环境变量 RADAR_PROXY > 系统 HTTPS_PROXY/HTTP_PROXY > 空（直连）。
# 也可在「设置」页填写并保存到 data/radar_proxy.txt。
_RADAR_PROXY_FILE = DATA_DIR / "radar_proxy.txt"

def _set_env_proxy(proxy: str) -> None:
    """把代理同步到环境变量，让 httpx 默认 trust_env 能读到。"""
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[k] = proxy
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)


def _load_radar_proxy() -> str:
    env = os.getenv("RADAR_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""
    if _RADAR_PROXY_FILE.exists():
        saved = _RADAR_PROXY_FILE.read_text(encoding="utf-8").strip()
        if saved:
            _set_env_proxy(saved)
            return saved
    return env

RADAR_PROXY = _load_radar_proxy()

def save_radar_proxy(proxy: str) -> None:
    """保存 Radar 代理地址到本地文件（非敏感信息），并同步到环境变量。"""
    global RADAR_PROXY
    RADAR_PROXY = proxy.strip()
    _set_env_proxy(RADAR_PROXY)
    if RADAR_PROXY:
        _RADAR_PROXY_FILE.write_text(RADAR_PROXY, encoding="utf-8")
    elif _RADAR_PROXY_FILE.exists():
        _RADAR_PROXY_FILE.unlink()


# ---------- IMA 知识库凭证（存储后端，敏感）----------
# 优先级（在 app/services/ima_client.py 中聚合）：
#   显式传入 > 环境变量 > 本文件持久化的 data/ima_creds.json。
# 环境变量名为 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY / IMA_KB_ID
# （兼容旧的 Client_ID / API_KEY / IMA_CLIENT_ID / IMA_API_KEY）。
# 服务器部署没有 .env 时，可在「设置」页填写，落到 data/ima_creds.json
# （该目录已被 .gitignore 忽略，不会入库）。
_IMA_CREDS_FILE = DATA_DIR / "ima_creds.json"


def load_ima_creds() -> dict:
    """返回已持久化的 IMA 凭证 {client_id, api_key, kb_id}（缺省为空）。"""
    if _IMA_CREDS_FILE.exists():
        try:
            d = json.loads(_IMA_CREDS_FILE.read_text(encoding="utf-8") or "{}")
            return {
                "client_id": str(d.get("client_id") or ""),
                "api_key": str(d.get("api_key") or ""),
                "kb_id": str(d.get("kb_id") or ""),
            }
        except Exception:
            return {"client_id": "", "api_key": "", "kb_id": ""}
    return {"client_id": "", "api_key": "", "kb_id": ""}


# 各凭证对应的环境变量名（按优先级）。与 ima_client.IMAClient 解析顺序保持一致，
# 这样「设置页」展示的来源才能反映运行时真实生效的来源。
_IMA_ENV_MAP = {
    "client_id": ("IMA_OPENAPI_CLIENTID", "IMA_CLIENT_ID", "Client_ID"),
    "api_key":   ("IMA_OPENAPI_APIKEY", "IMA_API_KEY", "API_KEY"),
    "kb_id":     ("IMA_KB_ID",),
}


def resolve_ima_creds() -> dict:
    """
    返回 IMA 凭证的「有效值来源」，不回传明文。

    解析优先级与 ima_client.IMAClient 一致：
        环境变量 > data/ima_creds.json（设置页持久化文件）。
    返回值（每个参数一个键，加统一前缀）：
        {client_id_set, client_id_src, client_id_env,
         api_key_set,   api_key_src,   api_key_env,
         kb_id_set,     kb_id_src,     kb_id_env}
    其中 *_src ∈ {"env", "settings", "none"}；*_env 记录命中的环境变量名
    （仅变量名，不含值，可安全返回前端展示来源）。
    """
    file_creds = load_ima_creds()
    out: Dict[str, object] = {}
    for key, names in _IMA_ENV_MAP.items():
        file_val = (file_creds.get(key) or "").strip()
        env_val = None
        env_name = None
        for n in names:
            v = os.getenv(n)
            if v and v.strip():
                env_val = v.strip()
                env_name = n
                break
        if env_val:
            out[f"{key}_set"] = True
            out[f"{key}_src"] = "env"
            out[f"{key}_env"] = env_name
        elif file_val:
            out[f"{key}_set"] = True
            out[f"{key}_src"] = "settings"
            out[f"{key}_env"] = ""
        else:
            out[f"{key}_set"] = False
            out[f"{key}_src"] = "none"
            out[f"{key}_env"] = ""
    return out


def save_ima_creds(client_id: str, api_key: str, kb_id: str) -> dict:
    """保存 IMA 凭证到 data/ima_creds.json。

    空字符串表示「不修改该项」（与 LLM 的 api_key 语义一致），
    仅当填写了新值才覆盖，方便只改其中一项。返回保存后的完整凭证。
    """
    cur = load_ima_creds()
    if client_id:
        cur["client_id"] = client_id.strip()
    if api_key:
        cur["api_key"] = api_key.strip()
    if kb_id:
        cur["kb_id"] = kb_id.strip()
    _IMA_CREDS_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(_IMA_CREDS_FILE, 0o600)
    return cur



# ---------- 设置页使用说明书（问号链接跳转地址） ----------
# 留空则前端不显示「?」图标；填写飞书云文档/其他说明页地址。
HELP_DOC_URL = os.getenv("SETTINGS_HELP_DOC_URL", "")


# ---------- 翻译任务并发上限 ----------
# 同时跑的后台翻译线程数上限（防止一次性发起大量任务把大模型 API / CPU 打满）。
# 超出上限的任务进入「排队中」状态，等前一个槽位空出再开始。
MAX_CONCURRENT_TRANSLATIONS = int(os.getenv("MAX_CONCURRENT_TRANSLATIONS", "3") or "3")
if MAX_CONCURRENT_TRANSLATIONS < 1:
    MAX_CONCURRENT_TRANSLATIONS = 1


# ---------- arXiv 检索源（可切换，默认可直连镜像，无需代理）----------
# 国内常见可直连镜像：上海交大、中科大。主站为官方。
# 设置页可切换；也可环境变量 ARXIV_SOURCE 或 data/arxiv_source.txt 指定 key。
ARXIV_SOURCES = {
    # key: (显示名, API base, 是否国内直连)
    "sjtu":      ("上海交大镜像 (国内直连)", "https://arxiv.sjtu.edu.cn/api/query", True),
    "ustc":      ("中科大镜像 (国内直连)",   "https://mirrors.ustc.edu.cn/arxiv-api/query", True),
    "export":    ("arXiv 主站 (export.arxiv.org)", "https://export.arxiv.org/api/query", False),
    "arxiv":     ("arXiv 主站 (arxiv.org)",      "https://arxiv.org/api/query", False),
}
ARXIV_SOURCE_DEFAULT = "sjtu"
_ARXIV_SOURCE_FILE = DATA_DIR / "arxiv_source.txt"

def _load_arxiv_source() -> str:
    env = os.getenv("ARXIV_SOURCE", "").strip()
    if env in ARXIV_SOURCES:
        return env
    if _ARXIV_SOURCE_FILE.exists():
        saved = _ARXIV_SOURCE_FILE.read_text(encoding="utf-8").strip()
        if saved in ARXIV_SOURCES:
            return saved
    return ARXIV_SOURCE_DEFAULT

ARXIV_SOURCE = _load_arxiv_source()

def arxiv_api_base() -> str:
    return ARXIV_SOURCES.get(ARXIV_SOURCE, ARXIV_SOURCES[ARXIV_SOURCE_DEFAULT])[1]

def save_arxiv_source(key: str) -> None:
    global ARXIV_SOURCE
    if key not in ARXIV_SOURCES:
        key = ARXIV_SOURCE_DEFAULT
    ARXIV_SOURCE = key
    _ARXIV_SOURCE_FILE.write_text(key, encoding="utf-8")


# ---------- 资讯检索语言/地区（可切换）----------
# Google News 按 hl/gl/ceid 决定返回语区。默认英文全球，拿国际资讯。
NEWS_LANGS = {
    "en":  ("English / 全球",   {"hl": "en-US", "gl": "US", "ceid": "US:en"}),
    "zh":  ("中文 / 中国",      {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}),
    "auto": ("跟随源（英文优先）", None),  # 英语失败回退中文
}


# ---------- 国内优质 AI / 科技媒体 RSS（国内直连，无需代理）----------
# 已实测可返回真实 RSS XML 的源（2026-08 验证）。
# 注意：微信公众号本身不提供官方 RSS，下方都是「官网/同步 RSS」，
# 最贴近「国内公众号文章」诉求的是 量子位 / 36氪 等科技媒体。
# 若想抓特定微信公众号，请用 RSSHub（见下方 RSSHUB_* 配置）。
CN_MEDIA_RSS = [
    ("量子位",       "https://www.qbitai.com/feed"),
    ("36氪",         "https://www.36kr.com/feed"),
    ("InfoQ",        "https://www.infoq.cn/feed"),
    ("少数派",       "https://sspai.com/feed"),
    ("阮一峰周刊",    "https://www.ruanyifeng.com/blog/atom.xml"),
]

# ---------- RSSHub：抓取微信公众号 / 任意站点的统一方案 ----------
# 微信公众号没有官方 RSS，标准做法是自建或借用 RSSHub 实例，
# 路由格式：{RSSHUB_BASE}/wechat/mp/{gh_id}，gh_id 即公众号原始 ID（gh_ 开头）。
# 公共实例多不稳定（常 503），建议用户在本地/服务器自建 RSSHub 后填入 base URL。
# 也可在「设置」页填写 base，并维护要抓的公众号列表（data/wechat_accounts.txt）。
RSSHUB_DEFAULT_BASE = "https://rsshub.app"   # 公共默认，可能不可用；可改为自建地址
_RSSHUB_BASE_FILE = DATA_DIR / "rsshub_base.txt"

def _load_rsshub_base() -> str:
    env = os.getenv("RSSHUB_BASE", "").strip()
    if env:
        return env.rstrip("/")
    if _RSSHUB_BASE_FILE.exists():
        saved = _RSSHUB_BASE_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved.rstrip("/")
    return RSSHUB_DEFAULT_BASE

RSSHUB_BASE = _load_rsshub_base()

def save_rsshub_base(base: str) -> None:
    """保存 RSSHub 实例 base URL（含协议，不含尾部斜杠）。"""
    global RSSHUB_BASE
    RSSHUB_BASE = (base or "").strip().rstrip("/")
    if RSSHUB_BASE:
        _RSSHUB_BASE_FILE.write_text(RSSHUB_BASE, encoding="utf-8")
    elif _RSSHUB_BASE_FILE.exists():
        _RSSHUB_BASE_FILE.unlink()


# ---------- 要抓取的微信公众号（按 gh_ 原始 ID 标识）----------
# 用户在「设置」页维护；每行 `名称|gh_id`。
# 例：机器之心|gh_4df112579a8c
_WECHAT_FILE = DATA_DIR / "wechat_accounts.txt"

def load_wechat_accounts() -> list:
    """读取要抓取的公众号列表 [(name, gh_id), ...]。"""
    out = []
    if _WECHAT_FILE.exists():
        for line in _WECHAT_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, gh = line.split("|", 1)
                out.append((name.strip(), gh.strip()))
            else:
                out.append(("公众号", line))
    return out

def save_wechat_accounts(accounts: list) -> None:
    lines = [f"{name}|{gh}" for name, gh in accounts if gh]
    _WECHAT_FILE.write_text("\n".join(lines), encoding="utf-8")


# ---------- 自定义 RSS（用户追加的任意 RSS 源）----------
_CN_RSS_FILE = DATA_DIR / "news_rss_feeds.txt"

def load_custom_rss() -> list:
    """读取用户自定义的 RSS 源（每行 `名称|URL`）。"""
    out = []
    if _CN_RSS_FILE.exists():
        for line in _CN_RSS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, url = line.split("|", 1)
                out.append((name.strip(), url.strip()))
            else:
                out.append(("自定义源", line))
    return out

def save_custom_rss(feeds: list) -> None:
    """保存用户自定义 RSS 源（[(name, url), ...]）。"""
    lines = []
    for name, url in feeds:
        lines.append(f"{name}|{url}")
    _CN_RSS_FILE.write_text("\n".join(lines), encoding="utf-8")

def all_cn_rss() -> list:
    """内置源 + 用户自定义源。"""
    return list(CN_MEDIA_RSS) + load_custom_rss()


# ---------- 默认检索模板（Research Radar 万用模板）----------
# 注意：paper 模板默认 time_range_days=7（细分方向 2 天内常无新提交，易返回空）。
DEFAULT_RADAR_TEMPLATES = [
    {"name": "VLA / WAM", "type": "paper", "field": "VLA", "keywords": "VLA, WAM, vision-language-action", "note": "视觉-语言-动作模型", "time_range_days": 7},
    {"name": "World Model", "type": "paper", "field": "World Model", "keywords": "world model, dreamer, generative model", "note": "世界模型", "time_range_days": 7},
    {"name": "Multimodal", "type": "paper", "field": "Multimodal", "keywords": "multimodal, VLN, MLLM", "note": "多模态", "time_range_days": 7},
    {"name": "Agent", "type": "paper", "field": "Agent", "keywords": "LLM agent, autonomous agent, tool use", "note": "智能体", "time_range_days": 7},
    {"name": "RAG", "type": "paper", "field": "RAG", "keywords": "retrieval-augmented generation, RAG", "note": "检索增强生成", "time_range_days": 7},
    {"name": "AI 公司上市 / 产业", "type": "news", "field": "AI 产业", "keywords": "AI startup funding OR AI IPO OR LLM funding OR generative AI investment OR AI company valuation", "note": "AI 产业动态", "lang": "en", "channel": "all", "time_range_days": 7},
    {"name": "具身智能落地", "type": "news", "field": "具身智能", "keywords": "humanoid robot OR embodied AI OR quadruped OR robotics company OR robot manufacturing", "note": "具身智能应用", "lang": "en", "channel": "all", "time_range_days": 7},
]
