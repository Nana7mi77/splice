"""Bounded DeepSeek JSON calls. No response body or credential is logged."""

from copy import deepcopy
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import validate_base_url
from .errors import SpliceError


class CloudError(SpliceError):
    def __init__(self, message, category="invalid_response"):
        super().__init__(message)
        self.category = category


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib can forward Authorization on redirects: never follow one.
        return None


def _strict_json(text):
    def invalid_constant(value):
        raise ValueError("Non-finite JSON value")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique)


def _transport_main():
    """Private subprocess protocol. Only its parent receives stdout.

    A separate process lets the parent stop DNS, TLS, headers, or body reads at
    one wall-clock deadline, even when a provider sends endless keepalive bytes.
    Credentials travel through stdin, never argv, environment, or a temp file.
    """
    try:
        payload = _strict_json(sys.stdin.buffer.read(1_000_000).decode("utf-8"))
        request = Request(payload["url"], data=payload["body"].encode("utf-8"), headers={
            "Authorization": "Bearer " + payload["key"], "Content-Type": "application/json",
        }, method="POST")
        with build_opener(_NoRedirect()).open(request, timeout=payload["timeout"]) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            result = {"error": "response_limit"}
        else:
            result = {"body": raw.decode("utf-8")}
    except HTTPError as exc:
        result = {"error": "http", "http_status": exc.code}
        exc.close()
    except TimeoutError:
        result = {"error": "timeout"}
    except (URLError, OSError):
        result = {"error": "network"}
    except Exception:
        # Never serialize exceptions: providers/proxies can echo credentials.
        result = {"error": "transport"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


def _post_with_deadline(url, encoded, key, timeout):
    command = [sys.executable, "-c",
               "import sys; sys.path.insert(0, sys.argv[1]); "
               "from splice.cloud import _transport_main; _transport_main()",
               str(Path(__file__).resolve().parents[1])]
    payload = json.dumps({"url": url, "body": encoded.decode("utf-8"), "key": key,
                          "timeout": timeout}, ensure_ascii=False).encode("utf-8")
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        output, _ = process.communicate(input=payload, timeout=remaining)
        if process.returncode != 0:
            raise CloudError("DeepSeek transport stopped without a response; no retry performed", "transport")
        result = _strict_json(output.decode("utf-8"))
        if not isinstance(result, dict):
            raise CloudError("DeepSeek transport returned an invalid result", "transport")
        error = result.get("error")
        if error == "http" and isinstance(result.get("http_status"), int):
            raise CloudError(f"DeepSeek HTTP {result['http_status']}; response body withheld, no retry performed", "http")
        if error in {"timeout", "network", "response_limit", "transport"}:
            raise CloudError("DeepSeek transport failed; response details withheld, no retry performed", error)
        if not isinstance(result.get("body"), str):
            raise CloudError("DeepSeek transport returned no response", "transport")
        return result["body"]
    except subprocess.TimeoutExpired:
        raise CloudError("DeepSeek total deadline exceeded; network worker stopped, no retry performed", "timeout") from None
    finally:
        if process.poll() is None:
            process.kill()
        # Reap the child and close all private credential/response pipes after
        # success, timeout, or interruption. SIGKILL closes the network socket.
        process.communicate()


class DeepSeekClient:
    """A client instance has a finite call budget, including failed attempts."""

    def __init__(self, store, offline=False):
        self.store = store
        self.offline = offline
        self.calls = []

    @property
    def metadata(self):
        return deepcopy(self.calls)

    @property
    def usage(self):
        totals = {}
        for call in self.calls:
            for key, count in call["usage"].items():
                totals[key] = totals.get(key, 0) + count
        return totals

    def complete_json(self, messages, max_tokens=None):
        if self.offline:
            raise CloudError("Cloud requests are disabled in offline mode")
        settings = self.store.load()["deepseek"]
        if len(self.calls) >= settings["max_calls"]:
            raise CloudError("DeepSeek call budget exhausted; no automatic retries")
        base_url = validate_base_url(settings["base_url"], settings["allow_custom_base_url"])
        limit = settings["max_tokens"] if max_tokens is None else max_tokens
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= settings["max_tokens"]:
            raise CloudError("Requested output tokens exceed the configured budget")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(message, dict) or set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message["content"], str) for message in messages
        ):
            raise CloudError("Messages must contain only role and text content")
        key = self.store.get_api_key()
        if not key:
            raise CloudError("DeepSeek credential is missing; run splice config deepseek")
        if not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key):
            raise CloudError("DeepSeek credential has an invalid format")
        body = {
            "model": settings["model"], "messages": messages,
            "response_format": {"type": "json_object"}, "max_tokens": limit,
            "thinking": {"type": "disabled"}, "stream": False,
        }
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 250000:
            raise CloudError("Cloud prompt exceeds the bounded input budget")
        event = {"model": settings["model"], "latency_seconds": 0, "usage": {}, "response_id": None,
                 "status": "started", "error_category": None}
        self.calls.append(event)
        started = time.monotonic()
        try:
            raw = _post_with_deadline(base_url + "/chat/completions", encoded, key, settings["timeout"])
            envelope = _strict_json(raw)
            if not isinstance(envelope, dict):
                raise CloudError("DeepSeek returned an invalid response envelope")
            response_id = envelope.get("id")
            if (isinstance(response_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", response_id)
                    and key not in response_id):
                event["response_id"] = response_id
            usage = envelope.get("usage")
            if isinstance(usage, dict):
                event["usage"] = {name: value for name, value in usage.items()
                                  if name in {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                                  and isinstance(value, (int, float)) and not isinstance(value, bool)
                                  and math.isfinite(value) and value >= 0}
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise CloudError("DeepSeek returned no single JSON completion")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise CloudError("DeepSeek completion was truncated or did not finish normally", "incomplete")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise CloudError("DeepSeek returned empty JSON content")
            if key in content:
                raise CloudError("DeepSeek response contained a credential and was discarded", "credential_echo")
            result = _strict_json(content)
            if not isinstance(result, dict):
                raise CloudError("DeepSeek JSON completion must be an object")
            if key in json.dumps(result, ensure_ascii=False):
                raise CloudError("DeepSeek response contained a credential and was discarded", "credential_echo")
            event["status"] = "completed"
            return result
        except CloudError as exc:
            event.update(status="failed", error_category=exc.category)
            raise
        except (URLError, TimeoutError, OSError):
            event.update(status="failed", error_category="network")
            raise CloudError("DeepSeek network or timeout failure; no retry performed", "network") from None
        except (ValueError, TypeError, UnicodeError, KeyError, IndexError):
            event.update(status="failed", error_category="invalid_response")
            raise CloudError("DeepSeek returned invalid JSON or an unsupported response shape") from None
        except BaseException:
            event.update(status="interrupted", error_category="interrupted")
            raise
        finally:
            event["latency_seconds"] = round(time.monotonic() - started, 3)
