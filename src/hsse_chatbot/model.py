"""Lazy local inference for the instruction-tuned Gemma 4 model."""

from __future__ import annotations

import json
from threading import Lock
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ChatbotConfig


class ChatGenerator(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> str:
        """Generate one assistant reply from chat-template messages."""


class ModelLoadError(RuntimeError):
    """Raised when the local model cannot be loaded or invoked."""


class LlamaCppServerGenerator:
    """Use a loopback llama.cpp OpenAI-compatible server for local GGUF inference."""

    def __init__(self, config: ChatbotConfig) -> None:
        self.config = config
        self._lock = Lock()

    def generate(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.config.model_id,
            "messages": messages,
            "max_tokens": self.config.max_new_tokens,
            "temperature": self.config.temperature,
            "stream": False,
            # Gemma 4 supports thinking, and recent llama.cpp builds may enable it
            # from the model's chat template even when the caller did not ask for
            # it.  With a bounded output budget, the hidden reasoning can consume
            # every token; llama.cpp then returns reasoning_content while content
            # is empty.  Set both supported request controls so normal chatbot
            # turns always leave room for the user-visible answer.
            "chat_template_kwargs": {
                "enable_thinking": self.config.enable_thinking,
            },
        }
        if not self.config.enable_thinking:
            payload["thinking_budget_tokens"] = 0
        request = Request(
            f"{self.config.model_base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._lock, urlopen(request, timeout=600) as response:
                result = json.loads(response.read().decode("utf-8"))
            message = result["choices"][0]["message"]
            content = self._content_text(message.get("content"))
            if not isinstance(content, str) or not content.strip():
                if message.get("reasoning_content"):
                    raise ValueError(
                        "the server used the output budget for hidden reasoning "
                        "and returned no final answer"
                    )
                finish_reason = result["choices"][0].get("finish_reason")
                detail = (
                    f" (finish_reason={finish_reason})" if finish_reason else ""
                )
                raise ValueError(f"the server returned an empty response{detail}")
            return content.strip()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise ModelLoadError(
                f"llama.cpp returned HTTP {error.code}: {detail}"
            ) from error
        except URLError as error:
            raise ModelLoadError(
                "Cannot reach the local llama.cpp server at "
                f"{self.config.model_base_url}. Start it with: "
                "llama serve -hf "
                "google/gemma-4-E4B-it-qat-q4_0-gguf:Q4_0"
            ) from error
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ModelLoadError(
                f"llama.cpp returned an invalid chat response: {error}"
            ) from error

    @classmethod
    def _content_text(cls, content: object) -> str:
        """Normalize OpenAI text content returned as a string or content parts."""

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""


class LocalGemmaGenerator:
    """Load Gemma on first use and serialize generation across UI sessions."""

    def __init__(self, config: ChatbotConfig) -> None:
        self.config = config
        self._processor: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._lock = Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> None:
        if self.loaded:
            return
        try:
            import torch
            from transformers import AutoModelForMultimodalLM, AutoProcessor

            processor = AutoProcessor.from_pretrained(
                self.config.model_id,
                local_files_only=self.config.local_files_only,
            )
            model = AutoModelForMultimodalLM.from_pretrained(
                self.config.model_id,
                dtype="auto",
                device_map="auto",
                local_files_only=self.config.local_files_only,
            )
            model.eval()
        except Exception as error:  # dependency, auth, download, or device failure
            mode_hint = (
                "HSSE_MODEL_LOCAL_ONLY is enabled; download the model first."
                if self.config.local_files_only
                else "Confirm the Hugging Face model terms, login, and available RAM."
            )
            raise ModelLoadError(
                f"Could not load {self.config.model_id}. {mode_hint} "
                f"Underlying error: {error}"
            ) from error
        self._torch = torch
        self._processor = processor
        self._model = model

    def generate(self, messages: list[dict[str, str]]) -> str:
        with self._lock:
            self._load()
            processor = self._processor
            model = self._model
            torch = self._torch
            try:
                inputs = processor.apply_chat_template(
                    messages,
                    tokenize=True,
                    return_dict=True,
                    return_tensors="pt",
                    add_generation_prompt=True,
                    enable_thinking=self.config.enable_thinking,
                ).to(model.device)
                input_length = inputs["input_ids"].shape[-1]
                generation_options: dict[str, object] = {
                    "max_new_tokens": self.config.max_new_tokens,
                    "do_sample": self.config.temperature > 0,
                }
                if self.config.temperature > 0:
                    generation_options["temperature"] = self.config.temperature
                with torch.inference_mode():
                    outputs = model.generate(**inputs, **generation_options)
                raw_response = processor.decode(
                    outputs[0][input_length:], skip_special_tokens=False
                )
                parsed = processor.parse_response(raw_response)
                response = self._response_text(parsed)
                if response:
                    return response.strip()
                return processor.decode(
                    outputs[0][input_length:], skip_special_tokens=True
                ).strip()
            except ModelLoadError:
                raise
            except Exception as error:
                raise ModelLoadError(f"Gemma generation failed: {error}") from error

    @classmethod
    def _response_text(cls, parsed: object) -> str:
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, dict):
            for key in ("final", "response", "text", "content"):
                if key in parsed:
                    text = cls._response_text(parsed[key])
                    if text:
                        return text
            for value in parsed.values():
                text = cls._response_text(value)
                if text:
                    return text
        if isinstance(parsed, (list, tuple)):
            parts = [cls._response_text(value) for value in parsed]
            return "\n".join(part for part in parts if part)
        return ""


def create_generator(config: ChatbotConfig) -> ChatGenerator:
    if config.backend == "llama-cpp":
        return LlamaCppServerGenerator(config)
    return LocalGemmaGenerator(config)
