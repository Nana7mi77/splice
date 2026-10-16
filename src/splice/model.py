"""Explicit, cached local inference backends; no cloud or Mock fallback.

The default Apple Silicon model card specifies mlx-vlm 0.3.12, not mlx-lm.
Linux CPU support is a separate development backend and is never M1 evidence.
"""

from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import time

try:
    import resource
except ImportError:  # Keep the base CLI usable on Windows without Unix metrics.
    resource = None

from .errors import ConfirmationRequired, SpliceError


MODELS = {
    "mlx-vlm": {
        "model_id": "mlx-community/Qwen3.5-2B-4bit",
        "source": "https://huggingface.co/mlx-community/Qwen3.5-2B-4bit",
        "conversion_library": "mlx-vlm 0.3.12",
        "estimated_download_gb": 1.8,
        "extra": "local",
    },
    "mlx-lm": {
        "model_id": "mlx-community/Qwen3-1.7B-4bit",
        "source": "https://huggingface.co/mlx-community/Qwen3-1.7B-4bit",
        "conversion_library": "mlx-lm 0.24.0",
        "estimated_download_gb": 1.2,
        "extra": "local",
    },
    "transformers": {
        "model_id": "Qwen/Qwen3-1.7B",
        "source": "https://huggingface.co/Qwen/Qwen3-1.7B",
        "conversion_library": "transformers >=4.51.0 (CPU BF16)",
        "estimated_download_gb": 4.1,
        "extra": "local-cpu",
    },
}


class ModelError(SpliceError):
    pass


def _versions() -> dict:
    versions = {}
    for package in ("huggingface-hub", "mlx", "mlx-vlm", "mlx-lm", "transformers", "torch"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return versions


def _host() -> dict:
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        memory = None
    if platform.system() == "Linux":
        try:
            limit = (Path("/sys/fs/cgroup/memory.max")).read_text().strip()
            if limit.isdigit():
                memory = min(memory, int(limit)) if memory else int(limit)
        except OSError:
            pass
    return {"system": platform.system(), "machine": platform.machine(), "memory_bytes": memory}


class ModelManager:
    """Download once with consent, then load only the recorded local snapshot."""

    def __init__(self, store):
        self.store = store
        self._loaded = None
        self._loaded_identity = None

    def _settings(self):
        return self.store.load().get("local", {})

    @staticmethod
    def _backend(backend):
        apple = platform.system() == "Darwin" and platform.machine() == "arm64"
        if backend == "auto":
            if not apple:
                raise ModelError(
                    "MLX requires Apple Silicon. On this development host explicitly select "
                    "--backend transformers (CPU); this does not validate M1 compatibility."
                )
            backend = "mlx-vlm"
        if backend not in MODELS:
            raise ModelError(f"Unknown local backend: {backend}")
        if backend.startswith("mlx-") and not apple:
            raise ModelError("The MLX backends require macOS Apple Silicon (arm64).")
        return backend

    def setup(self, backend="auto", model_id=None, yes=False) -> dict:
        backend = self._backend(backend)
        selection = MODELS[backend]
        model_id = model_id or selection["model_id"]
        if not yes:
            raise ConfirmationRequired(
                f"Download https://huggingface.co/{model_id} for {backend} "
                f"(default estimate {selection['estimated_download_gb']} GB)? "
                "Confirm once with model setup --yes."
            )
        try:
            from huggingface_hub import HfApi, snapshot_download
        except ImportError as exc:
            raise ModelError(f"Install splice-prototype[{selection['extra']}] before model setup.") from exc
        required = {"transformers": ("torch", "transformers"), "mlx-vlm": ("mlx-vlm",), "mlx-lm": ("mlx-lm",)}[backend]
        versions = _versions()
        if any(package not in versions for package in required):
            raise ModelError(f"Missing {backend} dependencies; install splice-prototype[{selection['extra']}].")
        try:
            info = HfApi().model_info(model_id, files_metadata=True)
            wanted = [item for item in info.siblings if item.rfilename.endswith((".json", ".safetensors", ".jinja", ".txt", ".model", ".md"))]
            size = sum(item.size or 0 for item in wanted)
            # Use the hub cache location, respecting the user's HF_HOME.
            from huggingface_hub.constants import HF_HUB_CACHE
            cache = Path(HF_HUB_CACHE)
            cache.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(cache).free < size + 512 * 1024**2:
                raise ModelError(f"Model download needs approximately {size / 1e9:.2f} GB plus 0.5 GB free space.")
            snapshot = snapshot_download(
                repo_id=model_id, revision=info.sha,
                allow_patterns=[item.rfilename for item in wanted], max_workers=2,
            )
            settings = {
                "backend": backend, "model_id": model_id, "revision": info.sha,
                "snapshot_path": str(Path(snapshot).resolve()),
                "source": f"https://huggingface.co/{model_id}/tree/{info.sha}",
                "download_bytes": size,
                "max_tokens": 1200, "max_input_tokens": 10000,
                "dtype": "bfloat16" if backend == "transformers" else "4bit",
                "setup_versions": versions,
            }
            self.store.update("local", settings)
            self._loaded = None
            return self.status()
        except SpliceError:
            raise
        except Exception as exc:
            raise ModelError(f"Local model download failed ({type(exc).__name__}); retry model setup after checking Hub access.") from exc

    def status(self) -> dict:
        settings = self._settings()
        snapshot = Path(settings.get("snapshot_path", "/nonexistent-splice-model"))
        weights = list(snapshot.glob("*.safetensors")) if snapshot.is_dir() else []
        return {
            "configured": bool(settings.get("model_id")),
            "cached": bool(weights and (snapshot / "config.json").is_file() and (snapshot / "tokenizer_config.json").is_file()),
            "local_model": settings.get("model_id"), "backend": settings.get("backend"),
            "revision": settings.get("revision"), "snapshot_path": settings.get("snapshot_path"),
            "source": settings.get("source"), "download_bytes": settings.get("download_bytes"),
            "dtype": settings.get("dtype"), "host": _host(), "versions": _versions(),
            "adaptation_mode": "dynamic_context", "weights_updated": False,
            "apple_silicon_validation": "not_run" if platform.system() != "Darwin" else "requires_model_test",
        }

    def generate(self, messages: list, max_tokens=None, offline=True) -> dict:
        """Inference is always from cache, including when offline=False.

        Network downloads are exclusively in setup(); generation cannot silently
        fetch weights, use a remote model, or turn into the deterministic adapter.
        """
        settings = self._settings()
        if not self.status()["cached"]:
            raise ModelError("No complete local model snapshot. Run model setup first.")
        backend = self._backend(settings["backend"])
        snapshot = settings["snapshot_path"]
        identity = (backend, snapshot, settings.get("revision"))
        limit = max_tokens or settings.get("max_tokens", 1200)
        started = time.perf_counter()
        try:
            if backend == "transformers":
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer
                if self._loaded_identity != identity:
                    torch.set_num_threads(min(4, os.cpu_count() or 1))
                    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
                    model = AutoModelForCausalLM.from_pretrained(
                        snapshot, local_files_only=True, trust_remote_code=False,
                        torch_dtype=torch.bfloat16, low_cpu_mem_usage=True,
                    ).to("cpu").eval()
                    self._loaded = (model, tokenizer)
                    self._loaded_identity = identity
                model, tokenizer = self._loaded
                prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                inputs = tokenizer(prompt, return_tensors="pt")
                prompt_tokens = inputs["input_ids"].shape[1]
                if prompt_tokens > settings.get("max_input_tokens", 10000):
                    raise ModelError(f"Local context has {prompt_tokens} tokens; shorten the retrieved capabilities.")
                with torch.inference_mode():
                    output = model.generate(**inputs, max_new_tokens=limit, do_sample=False, pad_token_id=tokenizer.eos_token_id)
                generated = output[0, prompt_tokens:]
                text = tokenizer.decode(generated, skip_special_tokens=True)
                tokens = int(generated.shape[0])
                peak = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if platform.system() == "Darwin" else 1024)
                        if resource is not None else None)
            elif backend == "mlx-lm":
                import mlx.core as mx
                from mlx_lm import generate, load
                from mlx_lm.sample_utils import make_sampler
                if self._loaded_identity != identity:
                    self._loaded = load(snapshot, tokenizer_config={"local_files_only": True, "trust_remote_code": False})
                    self._loaded_identity = identity
                model, tokenizer = self._loaded
                prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                prompt_tokens = len(tokenizer.encode(prompt))
                if prompt_tokens > settings.get("max_input_tokens", 10000):
                    raise ModelError("Local context exceeds configured max_input_tokens.")
                text = generate(model, tokenizer, prompt=prompt, max_tokens=limit, sampler=make_sampler(temp=0), verbose=False)
                tokens = len(tokenizer.encode(text))
                peak = mx.get_peak_memory()
            else:
                import mlx.core as mx
                from mlx_vlm import generate, load
                from mlx_vlm.prompt_utils import apply_chat_template
                if self._loaded_identity != identity:
                    self._loaded = load(snapshot, trust_remote_code=False, local_files_only=True)
                    self._loaded_identity = identity
                model, processor = self._loaded
                configuration = json.loads((Path(snapshot) / "config.json").read_text())
                prompt = apply_chat_template(processor, configuration, messages, num_images=0, enable_thinking=False)
                tokenizer = getattr(processor, "tokenizer", processor)
                prompt_tokens = len(tokenizer.encode(prompt))
                if prompt_tokens > settings.get("max_input_tokens", 10000):
                    raise ModelError("Local context exceeds configured max_input_tokens.")
                result = generate(model, processor, prompt=prompt, image=None, max_tokens=limit, temperature=0, verbose=False)
                text = result.text
                tokens = result.generation_tokens
                peak = mx.get_peak_memory()
        except SpliceError:
            raise
        except Exception as exc:
            raise ModelError(f"Real local inference failed in {backend} ({type(exc).__name__}); no fallback was used.") from exc
        return {
            "text": text, "model": settings["model_id"], "backend": backend,
            "revision": settings.get("revision"), "latency_seconds": round(time.perf_counter() - started, 3),
            "peak_memory_bytes": peak, "prompt_tokens": prompt_tokens, "generated_tokens": tokens,
            "offline": True, "weights_updated": False,
        }

    def test(self, registry, learning_store=None) -> dict:
        from .local_intent import LocalOnDeviceIntentAdapter
        adapter = LocalOnDeviceIntentAdapter(registry, learning_store, self)
        intent = adapter.parse("今晚想吃30元以内的清淡盖饭，不超餐饮预算，符合条件才生成草稿，别真正下单")
        return {"intent": intent, "inference": adapter.last_report}
