"""外部 AI 客户端（OpenAI 兼容 /chat/completions）。

密钥只从插件配置读取，不进数据库、不进日志。
transport 可注入，测试用假 transport，不触网。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class AIError(Exception):
    """AI 调用失败（网络、HTTP、超时、响应不是 JSON）。调用方负责重试。"""


def fetch_image_b64(url: str, timeout: int = 20, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, str]:
    """下载图片转 base64，供多模态 content 使用。返回 (mime, b64)。超限/失败抛 AIError。

    显式绕过系统代理：容器与图床之间直连更可靠。
    """
    import base64

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "campus-inbox/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            mime = resp.headers.get_content_type() or "image/jpeg"
            data = resp.read(max_bytes + 1)
    except Exception as e:
        raise AIError(f"图片下载失败: {e}") from e
    if len(data) > max_bytes:
        raise AIError(f"图片超过 {max_bytes // 1024 // 1024}MB")
    return mime, base64.b64encode(data).decode("ascii")


def load_image_b64(ref: str, timeout: int = 20, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, str]:
    """http 引用走下载；本地路径直接读文件。返回 (mime, b64)。"""
    if ref.startswith("http"):
        return fetch_image_b64(ref, timeout, max_bytes)
    import base64
    import os

    try:
        size = os.path.getsize(ref)
        if size > max_bytes:
            raise AIError(f"图片超过 {max_bytes // 1024 // 1024}MB")
        with open(ref, "rb") as f:
            data = f.read()
    except OSError as e:
        raise AIError(f"本地图片读取失败: {e}") from e
    mime = "image/png" if ref.lower().endswith(".png") else "image/jpeg"
    return mime, base64.b64encode(data).decode("ascii")


def _default_transport(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AIError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AIError(f"网络错误: {e}") from e
    except json.JSONDecodeError as e:
        raise AIError("响应不是合法 JSON") from e


class ExternalAI:
    """OpenAI 兼容 chat/completions 最小客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        transport=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport or _default_transport

    def chat(self, system: str, user, max_tokens: int = 2000) -> str:
        """返回助手文本内容；失败抛 AIError。

        user 可以是 str（纯文本）或 OpenAI 多模态 content 段列表
        [{"type": "text", ...}, {"type": "image_url", ...}]。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        data = self._transport(
            f"{self.base_url}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}"},
            payload,
            self.timeout,
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise AIError("响应缺少 choices[0].message.content") from e


def parse_json_object(text: str) -> dict:
    """从模型输出提取 JSON 对象，容忍 ```json 围栏。失败抛 AIError。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉首行围栏与结尾围栏
        lines = t.splitlines()
        lines = lines[1:] if lines else []
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise AIError(f"模型输出不是合法 JSON: {e}") from e
    if not isinstance(obj, dict):
        raise AIError("模型输出不是 JSON 对象")
    return obj
