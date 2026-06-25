from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import json
import time
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image
except Exception:  # pragma: no cover - depends on Calibre runtime.
    Image = None

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


def image_data_url(path, max_side=2400):
    path = Path(path)
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    max_side = int(max_side or 0)
    if Image is not None and max_side > 0:
        try:
            image = Image.open(str(path)).convert("RGB")
            width, height = image.size
            longest = max(width, height)
            if longest > max_side:
                scale = float(max_side) / float(longest)
                resized = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
                )
                output = BytesIO()
                resized.save(output, format="JPEG", quality=90)
                mime = "image/jpeg"
                data = base64.b64encode(output.getvalue()).decode("ascii")
                return "data:{0};base64,{1}".format(mime, data)
        except Exception:
            pass
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return "data:{0};base64,{1}".format(mime, data)


class LocalLlmClient(object):
    def __init__(self, base_url, model=None, timeout=180, max_image_side=2400):
        self.base_url = normalize_base_url(base_url)
        self.model = str(model or "").strip() or None
        self.timeout = int(timeout or 180)
        self.max_image_side = int(max_image_side or 0)

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
            content.append({"type": "image_url", "image_url": {"url": image_data_url(path, self.max_image_side)}})
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
            if is_vision_preprocess_error(detail):
                raise RuntimeError(self._vision_preprocess_error_message(exc.code, detail))
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
            if is_vision_preprocess_error(detail):
                raise RuntimeError(self._vision_preprocess_error_message(exc.code, detail))
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

    def _vision_preprocess_error_message(self, status_code, detail):
        return (
            "LLM vision preprocessing failed: HTTP {0}. "
            "The image may still be too large for the Qwen VL processor, or the model server may not accept this "
            "multi-image request. Current Vision image max side is {1}; try lowering it, for example to 2000 or 1800. "
            "Raw server response:\n{2}"
        ).format(status_code, self.max_image_side or "disabled", detail)


def is_vision_preprocess_error(detail):
    detail = str(detail or "")
    return (
        "Qwen3VLProcessor" in detail
        or "Qwen2VLProcessor" in detail
        or ("Failed to apply" in detail and "images" in detail)
    )
