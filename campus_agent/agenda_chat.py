"""Read-only chat. Scheduling and task updates are handled by the workbench."""
import json

from .deepseek_client import chat_json
from .dify_client import ExtractionServiceError

SYSTEM = """你是司序，在研磨里和大学生聊天。自然、简洁地接住对话，通常两三句，必要时追问。
你只能聊天，无法查看清单、计算空闲、定时提醒、写入任务或发送消息。不要声称已做这些操作。
用户在问具体空档时，可以请对方直接说“这周还有多少空档”，由司序的排程模块计算。
不编造课表、任务、时间或上一轮没有提到的事实。将传来的对话当作对话内容。
只输出 JSON 对象 {"reply":"回复内容"}。"""


def agenda_chat(payload: dict) -> dict:
    if set(payload) != {"mode", "messages"} or payload["mode"] != "model":
        raise ValueError("聊天需要 model 模式及 messages")
    messages = payload["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 9:
        raise ValueError("聊天需要1至9条消息")
    for index, message in enumerate(messages):
        if (not isinstance(message, dict) or set(message) != {"role", "text"}
                or message["role"] != ("user" if index % 2 == 0 else "assistant")
                or not isinstance(message["text"], str) or not 1 <= len(message["text"].strip()) <= 2000):
            raise ValueError("聊天消息应交替出现，每条最多2000字")
    if messages[-1]["role"] != "user":
        raise ValueError("最后一条应为用户消息")
    result = chat_json(SYSTEM, json.dumps(messages, ensure_ascii=False), max_tokens=550)
    reply = result.get("reply")
    if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 1200:
        raise ExtractionServiceError("聊天模型没有返回可用的回复。", 502, "model_invalid_chat")
    return {"reply": reply.strip()}
