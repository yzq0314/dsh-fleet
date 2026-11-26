"""Keep the model-visible conversation for a local Desktop handoff, outside Fleet's event DB."""

import asyncio
import hmac
import json

import httpx


class DesktopTranscript:
    def __init__(self):
        self.messages = []
        self.size = 0
        self.reason = None

    def observe(self, event):
        kind = event.get("type")
        if isinstance(event.get("surfaceOp"), dict):
            self.reason = "unsupported_history"
            self.messages.clear()
        if self.reason or kind not in ("user/message", "assistant/message", "tool/result"):
            return
        try:
            data = event.get("data", {})
            msg = data if kind == "user/message" else data.get("message", {})
            if kind == "user/message":
                origin = msg.get("source", {}).get("kind")
                if origin == "runtime-context":
                    return  # Desktop generates its own runtime context when resumed.
                if origin != "user":
                    raise ValueError
            row = {"id": msg["id"], "role": msg["role"], "time": event["time"], "content": []}
            if row["role"] != {"user/message": "user", "assistant/message": "assistant",
                               "tool/result": "tool"}[kind] or event.get("surfaceOp") != "append":
                raise ValueError
            for block in msg["content"]:
                if block["type"] == "text":
                    row["content"].append({"type": "text", "text": block["text"]})
                elif block["type"] == "tool-call" and row["role"] == "assistant":
                    row["content"].append({k: block[k] for k in ("type", "id", "name", "arguments")})
                elif block["type"] != "reasoning" and block["type"] != "thinking":
                    raise ValueError
            if row["role"] == "tool":
                row.update(call_id=msg["toolCallId"], is_error=msg["isError"])
            if row["role"] == "assistant" and not row["content"]:
                return
            self.size += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if self.size > 2 * 1024 * 1024 or len(self.messages) >= 1024:
                self.reason = "history_too_large"
            else:
                self.messages.append(row)
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
            self.reason = "unsupported_history"
        if self.reason:
            self.messages.clear()

    async def publish(self, task, config, route):
        unavailable = {"status": "unavailable"}
        if not getattr(route, "desktop_handoff", False) or not config.get("secret"):
            return {**unavailable, "reason": "desktop_unavailable"}
        if self.reason:
            return {**unavailable, "reason": self.reason}
        paired = hmac.digest(config["secret"].encode(), b"dsh-fleet/desktop/v1", "sha256").hex()
        model_token = route.environment["FLEET_MODEL_CAPABILITY"]
        auth = hmac.digest(paired.encode(), ("handoff/v1/" + model_token).encode(), "sha256").hex()
        body = {"task_id": task.id, "workspace": task.workspace, "messages": self.messages,
                "provider": route.environment["FLEET_SELECTED_PROVIDER"], "model": route.model}
        endpoint = route.environment["FLEET_MODEL_BRIDGE"] + "/handoff"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=12) as client:
            for attempt in range(2):
                try:
                    reply = await client.post(endpoint, headers={"Authorization": "Bearer " + auth},
                                              content=json.dumps(body, ensure_ascii=False).encode())
                    if reply.status_code == 409 and attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    if reply.status_code == 422:
                        return {**unavailable, "reason": "unsupported_history"}
                    reply.raise_for_status()
                    result = reply.json()
                    if (isinstance(result, dict) and isinstance(result.get("session_id"), str) and len(result["session_id"]) == 36
                            and isinstance(result.get("title"), str)):
                        return {"status": "ready", "session_id": result["session_id"],
                                "workspace": task.workspace, "title": result["title"][:80]}
                except (httpx.TransportError, ValueError):
                    if attempt == 0:
                        continue
                except httpx.HTTPStatusError:
                    pass
                break
        return {**unavailable, "reason": "desktop_handoff_failed"}
