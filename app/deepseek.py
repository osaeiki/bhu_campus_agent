"""DeepSeek 大模型客户端：OpenAI 兼容接口，支持流式输出与工具调用。"""
import json
from typing import AsyncIterator, Optional

import httpx

from . import config

CHAT_URL = f"{config.DEEPSEEK_BASE_URL}/chat/completions"


class DeepSeekError(RuntimeError):
    pass


async def stream_chat(
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    temperature: float = 0.3,
) -> AsyncIterator[dict]:
    """流式调用 DeepSeek，逐事件产出：
    {"type":"content_delta","delta":str}
    {"type":"tool_delta","index":int,"id":str,"name":str,"arguments_delta":str}
    {"type":"finish","finish_reason":str}
    """
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0)) as client:
        try:
            async with client.stream("POST", CHAT_URL, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")[:500]
                    raise DeepSeekError(f"DeepSeek 接口返回 {resp.status_code}：{body}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield {"type": "content_delta", "delta": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        yield {
                            "type": "tool_delta",
                            "index": tc.get("index", 0),
                            "id": tc.get("id") or "",
                            "name": fn.get("name") or "",
                            "arguments_delta": fn.get("arguments") or "",
                        }
                    if choice.get("finish_reason"):
                        yield {"type": "finish", "finish_reason": choice["finish_reason"]}
        except httpx.HTTPError as e:
            raise DeepSeekError(f"请求 DeepSeek 失败：{e}") from e


async def complete(messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
    """非流式兜底调用（流式失败或需要重试时使用）。返回完整 message。"""
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
        try:
            resp = await client.post(CHAT_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise DeepSeekError(f"请求 DeepSeek 失败：{e}") from e
    if resp.status_code != 200:
        raise DeepSeekError(f"DeepSeek 接口返回 {resp.status_code}：{resp.text[:500]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]
    except Exception as e:
        raise DeepSeekError(f"DeepSeek 返回格式异常：{data}") from e
