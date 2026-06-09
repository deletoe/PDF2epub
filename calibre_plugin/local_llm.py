from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import json
import time
from pathlib import Path

try:
    from urllib.error import HTTPError
    from urllib.error import URLError
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover - Python 2 compatibility for old Calibre builds.
    from urllib2 import HTTPError, Request, URLError, urlopen


def normalize_base_url(base_url):
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        return "http://127.0.0.1:8000/v1"
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return base_url


def image_data_url(path):
    path = Path(path)
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return "data:{0};base64,{1}".format(mime, data)


class LocalLlmClient(object):
    def __init__(self, base_url, model=None, timeout=180):
        self.base_url = normalize_base_url(base_url)
        self.model = str(model or "").strip() or None
        self.timeout = int(timeout or 180)

    def resolve_model(self):
        if self.model:
            return self.model
        req = Request(self.base_url + "/models")
        with urlopen(req, timeout=min(self.timeout, 30)) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = data.get("data") or []
        if not models:
            raise RuntimeError("No models returned by {0}/models".format(self.base_url))
        self.model = models[0].get("id")
        if not self.model:
            raise RuntimeError("The first /v1/models result has no id")
        return self.model

    def vision_chat(self, prompt, image_paths, max_tokens, temperature=0, stream_callback=None, cancel_callback=None):
        content = [{"type": "text", "text": prompt}]
        for path in image_paths:
            content.append({"type": "image_url", "image_url": {"url": image_data_url(path)}})
        payload = {
            "model": self.resolve_model(),
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "max_tokens": int(max_tokens or 2200),
            "stream": bool(stream_callback),
        }
        if stream_callback:
            return self._post_stream("/chat/completions", payload, stream_callback, cancel_callback)
        return self._post_json("/chat/completions", payload, cancel_callback)

    def text_chat(self, prompt, max_tokens=1000, temperature=0, stream_callback=None, cancel_callback=None):
        payload = {
            "model": self.resolve_model(),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": int(max_tokens or 1000),
            "stream": bool(stream_callback),
        }
        if stream_callback:
            return self._post_stream("/chat/completions", payload, stream_callback, cancel_callback)
        return self._post_json("/chat/completions", payload, cancel_callback)

    def _post_json(self, path, payload, cancel_callback=None):
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        req = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("LLM request failed: HTTP {0}\n{1}".format(exc.code, detail))
        except URLError as exc:
            reason = exc.reason if hasattr(exc, "reason") else str(exc)
            raise RuntimeError("LLM request failed: {0}".format(reason))
        choice = (data.get("choices") or [{}])[0]
        return {
            "text": ((choice.get("message") or {}).get("content") or ""),
            "usage": data.get("usage") or {},
            "raw": data,
        }

    def _post_stream(self, path, payload, stream_callback, cancel_callback=None):
        req = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        chunks = []
        usage = {}
        finish_reason = ""
        started = time.time()
        try:
            with urlopen(req, timeout=self.timeout) as response:
                for raw_line in response:
                    if cancel_callback and cancel_callback():
                        raise RuntimeError("Canceled by user")
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if event.get("usage"):
                        usage = event.get("usage") or usage
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason"):
                        finish_reason = str(choices[0].get("finish_reason") or "")
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content") or ""
                    if text:
                        chunks.append(text)
                        stream_callback(text)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("LLM stream failed: HTTP {0}\n{1}".format(exc.code, detail))
        except URLError as exc:
            reason = exc.reason if hasattr(exc, "reason") else str(exc)
            raise RuntimeError("LLM stream failed: {0}".format(reason))
        return {
            "text": "".join(chunks),
            "usage": usage,
            "seconds": time.time() - started,
            "raw": {"choices": [{"finish_reason": finish_reason}]},
        }
