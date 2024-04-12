import json
import random
import string
import time
import uuid

import httpx
from fastapi import HTTPException

from api.chat_completions import num_tokens_from_messages, model_system_fingerprint, model_proxy, \
    split_tokens_from_content
from utils.Logger import Logger
from utils.config import history_disabled, free35_base_url_list, proxy_url_list

moderation_message = "I'm sorry, I cannot provide or engage in any content related to pornography, violence, or any unethical material. If you have any other questions or need assistance, please feel free to let me know. I'll do my best to provide support and assistance."


async def stream_response(response, model, max_tokens):
    chat_id = f"chatcmpl-{''.join(random.choice(string.ascii_letters + string.digits) for _ in range(29))}"
    system_fingerprint_list = model_system_fingerprint.get(model, None)
    system_fingerprint = random.choice(system_fingerprint_list) if system_fingerprint_list else None
    created_time = int(time.time())
    completion_tokens = -1
    len_last_content = 0
    end = False
    async for chunk in response.aiter_lines():
        if end:
            yield f"data: [DONE]\n\n"
            break
        try:
            if chunk == "data: [DONE]":
                yield f"data: [DONE]\n\n"
            elif not chunk.startswith("data: "):
                continue
            else:
                chunk_old_data = json.loads(chunk[6:])
                finish_reason = None
                if chunk_old_data.get("type") == "moderation":
                    delta = {"role": "assistant", "content": moderation_message}
                    finish_reason = "moderation"
                    end = True
                elif chunk_old_data.get("message").get("status") == "in_progress":
                    content = chunk_old_data["message"]["content"]["parts"][0]
                    if not content:
                        delta = {"role": "assistant", "content": ""}
                    else:
                        delta = {"content": content[len_last_content:]}
                    len_last_content = len(content)
                    if completion_tokens == max_tokens:
                        delta = {}
                        finish_reason = "length"
                        end = True
                elif chunk_old_data.get("message").get("metadata").get("finish_details"):
                    delta = {}
                    finish_reason = "stop"
                    end = True
                else:
                    continue
                chunk_new_data = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "logprobs": None,
                            "finish_reason": finish_reason
                        }
                    ],
                    "system_fingerprint": system_fingerprint
                }
                completion_tokens += 1
                yield f"data: {json.dumps(chunk_new_data)}\n\n"
        except Exception:
            Logger.error(f"Error: {chunk}")
            continue


async def chat_response(resp, model, prompt_tokens, max_tokens):
    """
    组装对话响应
    :param resp: 响应数据
    :param model: 模型
    :param prompt_tokens: prompt token数
    :param max_tokens: 最大token数量
    :return:
    """
    last_resp = None
    for i in reversed(resp):
        if i != "data: [DONE]" and i.startswith("data: ") and '"message":' in i:
            try:
                last_resp = json.loads(i[6:])
                break
            except Exception:
                Logger.error(f"Error: {i}")
                continue
    usage, system_fingerprint, finish_reason, message = await init_param(last_resp, max_tokens, model, prompt_tokens)
    return {
        "id": f"chatcmpl-{''.join(random.choice(string.ascii_letters + string.digits) for _ in range(29))}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason
            }
        ],
        "usage": usage,
        "system_fingerprint": system_fingerprint,
        "conversation_id": last_resp["conversation_id"]
    }


async def init_param(last_resp, max_tokens, model, prompt_tokens):
    """
    组装参数
    :param last_resp: 最终响应
    :param max_tokens: 最大token数量
    :param model: 模型
    :param prompt_tokens: prompt token数
    :return: 对应参数
    """
    if last_resp.get("type") == "moderation":
        message_content = moderation_message
        completion_tokens = 53
        finish_reason = "moderation"
    else:
        message_content = last_resp["message"]["content"]["parts"][0]
        message_content, completion_tokens, finish_reason = split_tokens_from_content(message_content, max_tokens, model)
    message = {
        "role": "assistant",
        "content": message_content,
    }
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens
    }
    system_fingerprint_list = model_system_fingerprint.get(model, None)
    system_fingerprint = random.choice(system_fingerprint_list) if system_fingerprint_list else None
    return usage, system_fingerprint, finish_reason, message


def api_messages_to_chat(api_messages):
    chat_messages = []
    for api_message in api_messages:
        role = api_message.get('role')
        content = api_message.get('content')
        chat_message = {
            "id": f"{uuid.uuid4()}",
            "author": {"role": role},
            "content": {"content_type": "text", "parts": [content]},
            "metadata": {}
        }
        chat_messages.append(chat_message)
    return chat_messages


class ChatService:
    def __init__(self, data, access_token=None):
        if proxy_url_list:
            self.s = httpx.AsyncClient(timeout=30, verify=False, proxies=random.choice(proxy_url_list))
        else:
            self.s = httpx.AsyncClient(timeout=30, verify=False)
        self.free35_base_url = random.choice(free35_base_url_list)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
        self.access_token = access_token
        self.oai_device_id = str(uuid.uuid4())
        self.chat_token = None

        self.data = data
        self.conversation_id = self.data.get("conversation_id", None)
        self.model = self.data.get("model", "gpt-3.5-turbo-0125")
        self.api_messages = self.data.get("messages", [])
        self.prompt_tokens = num_tokens_from_messages(self.api_messages, self.model)
        self.max_tokens = self.data.get("max_tokens", 2147483647)

        self.headers = None
        self.chat_request = None

    async def get_chat_requirements(self):
        url = f'{self.free35_base_url}/sentinel/chat-requirements'
        headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json',
            'Oai-Device-Id': self.oai_device_id,
            'Oai-Language': 'en-US',
            'Origin': 'https://chat.openai.com',
            'Referer': 'https://chat.openai.com/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': self.user_agent
        }
        if self.access_token:
            headers['Authorization'] = f'Bearer {self.access_token}'
        try:
            r = await self.s.post(url, headers=headers, json={})
            if r.status_code == 200:
                resp = r.json()

                arkose = resp.get('arkose', {})
                arkose_required = arkose.get('required')
                if arkose_required:
                    arkose_dx = arkose.get("dx")
                    raise HTTPException(status_code=403, detail="Arkose required")

                turnstile = resp.get('turnstile', {})
                turnstile_required = turnstile.get('required')
                if turnstile_required:
                    raise HTTPException(status_code=403, detail="Turnstile required")

                self.chat_token = resp.get('token')
                if not self.chat_token:
                    raise HTTPException(status_code=502, detail=f"Failed to get chat token: {r.text}")
                return self.chat_token
            else:
                if "application/json" == r.headers.get("Content-Type", ""):
                    detail = r.json().get("detail", r.json())
                else:
                    detail = r.content

                if r.status_code == 403:
                    raise HTTPException(status_code=r.status_code, detail="cf-please-wait")
                elif r.status_code == 429:
                    raise HTTPException(status_code=r.status_code, detail="rate-limit")
                raise HTTPException(status_code=r.status_code, detail=detail)

        except HTTPException as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            Logger.error(f"Failed to request: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to request.")

    def prepare_send_conversation(self):
        self.headers = {
            'Accept': 'text/event-stream',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json',
            'Oai-Device-Id': self.oai_device_id,
            'Oai-Language': 'en-US',
            'Openai-Sentinel-Chat-Requirements-Token': self.chat_token,
            'Origin': 'https://chat.openai.com',
            'Referer': 'https://chat.openai.com/',
            'Sec-Ch-Ua': '"Microsoft Edge";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': self.user_agent
        }
        if self.access_token:
            self.headers['Authorization'] = f'Bearer {self.access_token}'
        chat_messages = api_messages_to_chat(self.api_messages)
        if "gpt-4" in self.data.get("model"):
            model = "gpt-4"
        else:
            model = "text-davinci-002-render-sha"
        parent_message_id = f"{uuid.uuid4()}"
        websocket_request_id = f"{uuid.uuid4()}"
        self.chat_request = {
            "action": "next",
            "messages": chat_messages,
            "parent_message_id": parent_message_id,
            "model": model,
            "timezone_offset_min": -480,
            "suggestions": [],
            "history_and_training_disabled": history_disabled,
            "conversation_mode": {"kind": "primary_assistant"},
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_nulligen": False,
            "force_rate_limit": False,
            "websocket_request_id": websocket_request_id,
        }
        if self.conversation_id:
            self.chat_request["conversation_id"] = self.conversation_id
        return self.chat_request

    async def send_conversation_for_stream(self):
        url = f'{self.free35_base_url}/conversation'
        model = model_proxy.get(self.model, self.model)
        r = await self.s.send(
            self.s.build_request("POST", url, headers=self.headers, json=self.chat_request, timeout=600), stream=True)
        if r.status_code == 200:
            return stream_response(r, model, self.max_tokens)
        else:
            await r.aread()
            await self.parse_response_content(r)

    async def parse_response_content(self, r):
        if "application/json" == r.headers.get("Content-Type", ""):
            detail = r.json().get("detail", r.json())
        else:
            detail = r.content
        if r.status_code != 200:
            if r.status_code == 403:
                raise HTTPException(status_code=r.status_code, detail="cf-please-wait")
            raise HTTPException(status_code=r.status_code, detail=detail)

    async def send_conversation(self):
        url = f'{self.free35_base_url}/conversation'
        model = model_proxy.get(self.model, self.model)
        r = await self.s.send(
            self.s.build_request("POST", url, headers=self.headers, json=self.chat_request, timeout=600), stream=False)
        await self.parse_response_content(r)
        resp = r.text.split("\n")
        return await chat_response(resp, model, self.prompt_tokens, self.max_tokens)
