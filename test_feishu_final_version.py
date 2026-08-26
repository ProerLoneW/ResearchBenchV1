import os
import json
import time
import secrets
import threading
import webbrowser

from urllib.parse import urlencode, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

BASE_URL = "https://open.feishu.cn/open-apis"

REDIRECT_URI = "http://127.0.0.1:8765/callback"
TOKEN_FILE = "feishu_user_token.json"

# 安全提示：不要在代码中硬编码 Secret。请通过环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 提供。
APP_ID = os.getenv("FEISHU_APP_ID", 'cli_aa0ec67a13789bd8')
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

OAUTH_SCOPES = [
    "docx:document:create",
    "docx:document:write_only",
    "docx:document:readonly",
    "offline_access",
]


def check_response(resp):
    print("\n========== Feishu API Response ==========")
    print("HTTP Status:", resp.status_code)
    print("URL:", resp.url)
    print("Response Body:", resp.text)
    print("=========================================\n")

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(
            f"Feishu API returned non-JSON response\n"
            f"HTTP {resp.status_code}\n"
            f"{resp.text}"
        )

    if resp.status_code >= 400 or data.get("code", 0) != 0:
        raise RuntimeError(
            "Feishu API request failed:\n"
            f"HTTP Status: {resp.status_code}\n"
            f"Feishu Code: {data.get('code')}\n"
            f"Message: {data.get('msg')}\n"
            f"Full Response: {json.dumps(data, ensure_ascii=False, indent=2)}"
        )

    return data

def transfer_document_owner(
    token,
    document_id,
    user_open_id,
    remove_old_owner=True,
):
    url = (
        f"{BASE_URL}/drive/v1/permissions/"
        f"{document_id}/members/transfer_owner"
    )

    params = {
        "type": "docx",
        "need_notification": "true",
        "remove_old_owner": str(remove_old_owner).lower(),
    }

    payload = {
        "member_type": "openid",
        "member_id": user_open_id,
    }

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        params=params,
        json=payload,
        timeout=30,
    )

    return check_response(resp)

def get_tenant_access_token():
    """
    使用 App ID + App Secret 获取应用身份 token。
    """
    url = f"{BASE_URL}/auth/v3/tenant_access_token/internal"

    resp = requests.post(
        url,
        json={
            "app_id": APP_ID,
            "app_secret": APP_SECRET,
        },
        timeout=30,
    )

    data = check_response(resp)

    return data["tenant_access_token"]

def get_authorization_code():
    """
    打开浏览器让用户登录飞书并授权，
    本地临时监听 callback 获取 authorization code。
    """

    state = secrets.token_urlsafe(24)

    params = {
        "client_id": APP_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(OAUTH_SCOPES),
        "state": state,
    }

    auth_url = (
        "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
        + urlencode(params)
    )

    result = {
        "code": None,
        "error": None,
    }

    class CallbackHandler(BaseHTTPRequestHandler):

        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            query = parse_qs(parsed.query)

            returned_state = query.get("state", [None])[0]

            if returned_state != state:
                result["error"] = "OAuth state mismatch"

            elif "code" in query:
                result["code"] = query["code"][0]

            else:
                result["error"] = query.get(
                    "error",
                    ["Unknown OAuth error"]
                )[0]

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.end_headers()

            self.wfile.write(
                """
                <html>
                <body>
                    <h2>飞书授权完成</h2>
                    <p>可以关闭这个页面，返回终端。</p>
                </body>
                </html>
                """.encode("utf-8")
            )

        def log_message(self, format, *args):
            # 不打印 HTTP server 日志
            pass

    server = HTTPServer(
        ("127.0.0.1", 8765),
        CallbackHandler,
    )

    print("\n请在浏览器完成飞书授权：")
    print(auth_url)
    print()

    webbrowser.open(auth_url)

    # 等一次 OAuth callback
    server.handle_request()
    server.server_close()

    if result["error"]:
        raise RuntimeError(
            f"Feishu OAuth failed: {result['error']}"
        )

    if not result["code"]:
        raise RuntimeError(
            "没有从飞书获得 authorization code"
        )

    return result["code"]

def exchange_code_for_user_token(code):
    """
    authorization code -> user_access_token
    """

    url = f"{BASE_URL}/authen/v2/oauth/token"

    payload = {
        "grant_type": "authorization_code",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    resp = requests.post(
        url,
        headers={
            "Content-Type": "application/json; charset=utf-8"
        },
        json=payload,
        timeout=30,
    )

    data = check_response(resp)

    # 兼容不同版本可能存在 data 包装
    token_data = data.get("data", data)

    access_token = token_data.get("access_token")

    if not access_token:
        raise RuntimeError(
            f"未获得 user_access_token: {data}"
        )

    return token_data

def save_user_token(token_data):

    expires_in = token_data.get(
        "expires_in",
        6900
    )

    token_data["expires_at"] = (
        int(time.time()) + expires_in
    )

    with open(
        TOKEN_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            token_data,
            f,
            ensure_ascii=False,
            indent=2,
        )
        
def load_user_token():

    if not os.path.exists(TOKEN_FILE):
        return None

    with open(
        TOKEN_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)

def get_user_access_token():

    token_data = load_user_token()

    # ==========================
    # 1. 已有 token 且没过期
    # ==========================

    if token_data:

        access_token = token_data.get(
            "access_token"
        )

        expires_at = token_data.get(
            "expires_at",
            0
        )

        # 提前 5 分钟刷新
        if (
            access_token
            and time.time() < expires_at - 300
        ):
            return access_token


        # ==========================
        # 2. 尝试 refresh
        # ==========================

        refresh_token = token_data.get(
            "refresh_token"
        )

        if refresh_token:

            print(
                "user_access_token 已过期，"
                "正在自动刷新..."
            )

            try:

                new_token_data = (
                    refresh_user_access_token(
                        refresh_token
                    )
                )

                print(
                    "user_access_token 刷新成功"
                )

                return new_token_data[
                    "access_token"
                ]

            except Exception as e:

                print(
                    "refresh token 失败，"
                    "重新进行用户授权：",
                    e
                )


    # ==========================
    # 3. 第一次 OAuth
    # ==========================

    print("需要进行飞书用户授权")

    code = get_authorization_code()

    token_data = (
        exchange_code_for_user_token(code)
    )

    save_user_token(token_data)

    print("user_access_token 获取成功")

    return token_data["access_token"]

def refresh_user_access_token(refresh_token):
    """
    refresh_token -> 新 user_access_token
    """

    url = f"{BASE_URL}/authen/v2/oauth/token"

    payload = {
        "grant_type": "refresh_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "refresh_token": refresh_token,
    }

    resp = requests.post(
        url,
        headers={
            "Content-Type": "application/json; charset=utf-8"
        },
        json=payload,
        timeout=30,
    )

    data = check_response(resp)

    token_data = data.get("data", data)

    if not token_data.get("access_token"):
        raise RuntimeError(
            f"刷新 user_access_token 失败: {data}"
        )

    save_user_token(token_data)

    return token_data

def create_document(token, title):
    """
    创建一个新的飞书新版云文档。
    """
    url = f"{BASE_URL}/docx/v1/documents"

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "title": title
        },
        timeout=30,
    )

    data = check_response(resp)

    document = data["data"]["document"]

    return document["document_id"]


def text_block(text):
    """
    普通文本 Block。
    block_type = 2
    """
    return {
        "block_type": 2,
        "text": {
            "elements": [
                {
                    "text_run": {
                        "content": text
                    }
                }
            ]
        }
    }


def heading1_block(text):
    """
    一级标题。
    block_type = 3
    """
    return {
        "block_type": 3,
        "heading1": {
            "elements": [
                {
                    "text_run": {
                        "content": text
                    }
                }
            ]
        }
    }


def append_blocks(token, document_id, blocks):
    """
    在文档根节点最后追加 Blocks。

    document_id 同时也是根 block_id。
    """
    url = (
        f"{BASE_URL}/docx/v1/documents/"
        f"{document_id}/blocks/{document_id}/children"
    )

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "children": blocks
        },
        timeout=30,
    )

    return check_response(resp)


def append_text(token, document_id, text):
    """
    单独追加一段文字。
    """
    return append_blocks(
        token,
        document_id,
        [
            text_block(text)
        ],
    )


# def main():

#     # 1. 获取 token
#     # token = get_tenant_access_token()
#     token = get_user_access_token()

#     # print("tenant_access_token 获取成功")
#     print("user_access_token 准备完成")

#     # 2. 创建云文档
#     document_id = create_document(
#         token,
#         "AI Research Workbench - 飞书 API 测试"
#     )

#     print("文档创建成功")
#     print("document_id:", document_id)

#     # 3. 第一次写入
#     append_blocks(
#         token,
#         document_id,
#         [
#             heading1_block("论文简介"),

#             text_block(
#                 "这是一篇通过 Python 调用飞书 OpenAPI "
#                 "自动创建的云文档。"
#             ),

#             text_block(
#                 "后续这里会被替换成论文 TeX Source "
#                 "解析得到的中文正文。"
#             ),
#         ],
#     )

#     print("第一次内容写入成功")

#     # 4. 再追加一段
#     append_text(
#         token,
#         document_id,
#         "这句话是第二次 API 调用追加进去的。"
#     )

#     print("追加内容成功")

#     print()
#     print("最终 document_id:")
#     print(document_id)
    
#     # print("转让文档给 owner")
    
#     # print()
#     # transfer_document_owner(
#     #     token,
#     #     document_id,
#     #     OWNER_OPEN_ID,
#     # )
    
#     # print("转让文档成功")

def main():

    # 1. 获取“用户身份” token
    token = get_user_access_token()

    print("user_access_token 准备完成")

    # 2. 以我的个人账号身份创建文档
    document_id = create_document(
        token,
        "AI Research Workbench - 飞书 API 测试"
    )

    print("文档创建成功")
    print("document_id:", document_id)

    # 3. 写内容
    append_blocks(
        token,
        document_id,
        [
            heading1_block("论文简介"),

            text_block(
                "这是以我的个人飞书账号身份"
                "自动创建的云文档。"
            ),

            text_block(
                "以后这里会写入中文 TeX "
                "Source 解析得到的正文。"
            ),
        ],
    )

    # 4. 继续追加
    append_text(
        token,
        document_id,
        "这是第二次 API 调用追加的内容。"
    )

    print("内容写入成功")
    print("document_id:", document_id)


if __name__ == "__main__":
    main()