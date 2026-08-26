"""
飞书客户端：封装 OAuth 用户授权、文档创建、Block 写入、图片上传。
逻辑来自 test_feishu_final_version.py，参数化以便后台设置与 TeX→飞书 工具复用。
不依赖任何大模型。
"""
import os
import json
import time
import secrets
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from ..config import (
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BASE_URL,
    FEISHU_REDIRECT_URI, FEISHU_TOKEN_FILE,
)


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, app_id=None, app_secret=None, token_file=None,
                 redirect_uri=None, scopes=None):
        self.app_id = app_id or FEISHU_APP_ID
        self.app_secret = app_secret or FEISHU_APP_SECRET
        self.base_url = FEISHU_BASE_URL
        self.token_file = token_file or FEISHU_TOKEN_FILE
        self.redirect_uri = redirect_uri or FEISHU_REDIRECT_URI
        self.scopes = scopes or [
            "docx:document:create",
            "docx:document:write_only",
            "docx:document:readonly",
            "drive.file:upload",
            "drive:drive:upload",
            "offline_access",
        ]

    # ---------------- 底层 ----------------
    def _check(self, resp):
        try:
            data = resp.json()
        except Exception:
            raise FeishuError(
                f"飞书返回非 JSON 响应\nHTTP {resp.status_code}\n{resp.text}"
            )
        if resp.status_code >= 400 or data.get("code", 0) != 0:
            raise FeishuError(
                "飞书请求失败:\n"
                f"HTTP {resp.status_code}\n"
                f"code={data.get('code')} msg={data.get('msg')}\n"
                f"{json.dumps(data, ensure_ascii=False, indent=2)}"
            )
        return data

    # ---------------- Token ----------------
    def _save_token(self, token_data):
        expires_in = token_data.get("expires_in", 6900)
        token_data["expires_at"] = int(time.time()) + expires_in
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False, indent=2)

    def _load_token(self):
        if not os.path.exists(self.token_file):
            return None
        with open(self.token_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _get_auth_code(self):
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": self.app_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
        }
        auth_url = (
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
            + urlencode(params)
        )
        result = {"code": None, "error": None}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404); self.end_headers(); return
                q = parse_qs(parsed.query)
                if q.get("state", [None])[0] != state:
                    result["error"] = "OAuth state mismatch"
                elif "code" in q:
                    result["code"] = q["code"][0]
                else:
                    result["error"] = q.get("error", ["Unknown"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h2>飞书授权完成</h2>"
                    "<p>可以关闭此页面，返回终端。</p></body></html>"
                    .encode("utf-8")
                )
            def log_message(self, *a): pass

        server = HTTPServer(("127.0.0.1", 8765), Handler)
        print("\n请在浏览器完成飞书授权：\n" + auth_url + "\n")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        server.handle_request()
        server.server_close()
        if result["error"]:
            raise FeishuError(f"飞书 OAuth 失败: {result['error']}")
        if not result["code"]:
            raise FeishuError("未获得 authorization code")
        return result["code"]

    def _exchange(self, code):
        resp = requests.post(
            f"{self.base_url}/authen/v2/oauth/token",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "grant_type": "authorization_code",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            }, timeout=30,
        )
        data = self._check(resp)
        return data.get("data", data)

    def _refresh(self, refresh_token):
        resp = requests.post(
            f"{self.base_url}/authen/v2/oauth/token",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "grant_type": "refresh_token",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "refresh_token": refresh_token,
            }, timeout=30,
        )
        data = self._check(resp)
        return data.get("data", data)

    def get_user_access_token(self):
        """返回有效的 user_access_token（自动刷新 / 首次授权）。"""
        tok = self._load_token()
        if tok:
            access = tok.get("access_token")
            exp = tok.get("expires_at", 0)
            if access and time.time() < exp - 300:
                return access
            rt = tok.get("refresh_token")
            if rt:
                try:
                    new = self._refresh(rt)
                    self._save_token(new)
                    return new["access_token"]
                except Exception as e:
                    print("refresh 失败，重新授权：", e)
        code = self._get_auth_code()
        td = self._exchange(code)
        self._save_token(td)
        return td["access_token"]

    # ---------------- 文档 ----------------
    def create_document(self, title):
        token = self.get_user_access_token()
        resp = requests.post(
            f"{self.base_url}/docx/v1/documents",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"title": title}, timeout=30,
        )
        data = self._check(resp)
        return data["data"]["document"]["document_id"]

    def append_blocks(self, document_id, blocks):
        token = self.get_user_access_token()
        url = (
            f"{self.base_url}/docx/v1/documents/"
            f"{document_id}/blocks/{document_id}/children"
        )
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"children": blocks}, timeout=60,
        )
        return self._check(resp)

    def upload_image_token(self, image_path):
        """上传图片到飞书云空间，返回 file_token（用于插入 image block）。"""
        token = self.get_user_access_token()
        if not os.path.exists(image_path):
            raise FeishuError(f"图片不存在: {image_path}")
        url = f"{self.base_url}/drive/v1/files/upload_all"
        fname = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            files = {"file": (fname, f, "application/octet-stream")}
            data = {
                "file_name": fname,
                "parent_type": "explorer",
                "parent_node": "root",
                "size": str(os.path.getsize(image_path)),
            }
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                files=files, data=data, timeout=60,
            )
        data = self._check(resp)
        return data["data"]["file_token"]

    # ---------------- Block 构造 ----------------
    @staticmethod
    def text_block(text, bold=False, italic=False):
        style = {}
        if bold: style["bold"] = True
        if italic: style["italic"] = True
        el = {"text_run": {"content": text}}
        if style:
            el["text_run"]["text_element_style"] = style
        return {"block_type": 2, "text": {"elements": [el],
                "style": {"align": "left"}}}

    @staticmethod
    def heading_block(level, text):
        bt = {1: 3, 2: 4, 3: 5}.get(level, 3)
        return {f"block_type": bt, f"heading{level}": {
            "elements": [{"text_run": {"content": text}}]}}

    @staticmethod
    def code_block(text):
        # code block（公式 / 代码用）
        return {"block_type": 14, "code": {
            "elements": [{"text_run": {"content": text}}],
            "language": 1, "style": {"align": "left"}}}

    @staticmethod
    def image_block(file_token, width=800):
        return {"block_type": 23, "image": {
            "token": file_token,
            "align": "center",
            "width": width,
        }}

    @staticmethod
    def divider_block():
        return {"block_type": 17, "divider": {"style": 1}}

    @staticmethod
    def bullet_block(text):
        return {"block_type": 12, "bullet": {
            "elements": [{"text_run": {"content": text}}],
            "style": {"align": "left"}}}

    @staticmethod
    def ordered_block(text):
        return {"block_type": 13, "ordered": {
            "elements": [{"text_run": {"content": text}}],
            "style": {"align": "left"}}}

    # ---------------- 便捷写入 ----------------
    def write_document(self, title, blocks):
        """创建文档并写入全部 blocks，返回 (document_id, url)。"""
        doc_id = self.create_document(title)
        # 分批发，避免单次过大
        for i in range(0, len(blocks), 50):
            self.append_blocks(doc_id, blocks[i:i + 50])
        return doc_id, f"https://bytedance.feishu.cn/docx/{doc_id}"

    # ---------------- 读取文档内容（转为 Markdown） ----------------
    def _doc_id_from_url(self, url_or_id: str) -> str:
        s = url_or_id.strip()
        # 形如 https://bytedance.feishu.cn/docx/{id} 或 /wiki/...
        if "docx/" in s:
            return s.split("docx/", 1)[1].split("?")[0].split("/")[0]
        if "wiki/" in s:
            return s.split("wiki/", 1)[1].split("?")[0].split("/")[0]
        return s  # 已是 document_id

    def fetch_document_markdown(self, url_or_id: str) -> str:
        """读取一篇飞书云文档（docx），将其 block 结构转换为 Markdown 文本。"""
        doc_id = self._doc_id_from_url(url_or_id)
        self.get_user_access_token()  # 确保已授权
        blocks = self._list_blocks(doc_id, doc_id)
        lines = []
        for b in blocks:
            md = self._block_to_markdown(b)
            if md:
                lines.append(md)
        return "\n\n".join(lines).strip() + "\n"

    def _list_blocks(self, document_id: str, block_id: str, page_token=""):
        """分页拉取 block 子节点（飞书单次最多 500）。"""
        token = self.get_user_access_token()
        out = []
        while True:
            url = (
                f"{self.base_url}/docx/v1/documents/{document_id}"
                f"/blocks/{block_id}/children"
            )
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"page_size": 500, "page_token": page_token},
                timeout=30,
            )
            data = self._check(resp)["data"]
            items = data.get("items", [])
            out.extend(items)
            page_token = data.get("page_token", "")
            if not data.get("has_more", False) or not page_token:
                break
        return out

    @staticmethod
    def _text_of(block: dict) -> str:
        """从任意 block 中提取纯文本。"""
        for key in ("text", "heading1", "heading2", "heading3", "heading4",
                    "heading5", "heading6", "bullet", "ordered", "code",
                    "quote", "callout", "caption"):
            node = block.get(key)
            if not node:
                continue
            parts = []
            for el in node.get("elements", []):
                content = (el.get("text_run") or {}).get("content", "")
                if content:
                    parts.append(content)
            if parts:
                return "".join(parts).strip()
        return ""

    def _block_to_markdown(self, block: dict) -> str:
        bt = block.get("block_type")
        if bt in (3, 4, 5, 6, 7, 8):  # heading1..6
            level = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6}[bt]
            return "#" * level + " " + self._text_of(block)
        if bt == 2:  # 正文
            return self._text_of(block)
        if bt == 12:  # 无序列表
            return "- " + self._text_of(block)
        if bt == 13:  # 有序列表
            return "1. " + self._text_of(block)
        if bt == 14:  # 代码块
            return "```\n" + self._text_of(block) + "\n```"
        if bt == 17:  # 分割线
            return "---"
        if bt == 23:  # 图片
            tok = block.get("image", {}).get("token", "")
            return f"![image](feishu-image:{tok})"
        if bt == 21:  # 引用
            return "> " + self._text_of(block)
        return self._text_of(block)
