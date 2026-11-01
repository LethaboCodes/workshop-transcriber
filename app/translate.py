"""Replaceable NLLB CPU/int8 translator. Importing this module loads no models."""
import hashlib
import os
import re
import threading
from pathlib import Path

_lock = threading.RLock()
_translator = None
_identity = None
_tokenizers = {}


def _load(src_lang):
    global _translator, _identity
    import ctranslate2
    from huggingface_hub import snapshot_download
    from transformers import AutoTokenizer
    from app.inference import cache_dir, config
    settings = config()["translation"]
    source_id = settings["id"]
    converted_id = settings.get("ct2_id")
    identity = (source_id, converted_id)
    storage = cache_dir()
    if _translator is None or identity != _identity:
        if converted_id:
            # Published int8 conversion avoids installing PyTorch in the web image.
            model_path = snapshot_download(converted_id, cache_dir=str(storage), token=os.environ.get("HF_TOKEN"),
                                           allow_patterns=["model.bin", "config.json", "*vocabulary*", "*.txt"])
        else:
            # Optional offline/custom-model conversion runs in the worker environment.
            model_path = str(storage / ("translation-" + hashlib.sha256(source_id.encode()).hexdigest()[:16]))
            if not (Path(model_path) / "model.bin").exists():
                try:
                    import torch  # Only needed when converting new source weights.
                except ImportError as exc:
                    raise RuntimeError("Set translation.ct2_id to a converted model in models.yaml, or convert the source once using the worker environment.") from exc
                converter = ctranslate2.converters.TransformersConverter(source_id)
                converter.convert(model_path, quantization="int8", force=True)
        _translator = ctranslate2.Translator(model_path, device="cpu", compute_type="int8",
                                            intra_threads=max(1, min(8, os.cpu_count() or 4)))
        _identity = identity
        _tokenizers.clear()
    code = settings["languages"].get(src_lang)
    if not code:
        raise ValueError(f"No translation language code is configured for {src_lang}.")
    if code not in _tokenizers:
        _tokenizers[code] = AutoTokenizer.from_pretrained(source_id, src_lang=code, cache_dir=str(storage),
                                                         token=os.environ.get("HF_TOKEN"))
    return _translator, _tokenizers[code], settings["target_language"]


def _clauses(text, tokenizer, limit=350):
    clauses = re.split(r"(?<=[.!?;:,])\s+|\n+", text.strip())
    pending = ""
    for clause in clauses:
        if not clause:
            continue
        joined = (pending + " " + clause).strip()
        if len(tokenizer.encode(joined, add_special_tokens=False)) <= limit:
            pending = joined
            continue
        if pending:
            yield pending
            pending = ""
        # Split overlong clauses at word boundaries, then token boundaries only for
        # individual words longer than the model input budget.
        for word in clause.split():
            joined = (pending + " " + word).strip()
            if len(tokenizer.encode(joined, add_special_tokens=False)) <= limit:
                pending = joined
                continue
            if pending:
                yield pending
            encoded = tokenizer.encode(word, add_special_tokens=False)
            if len(encoded) > limit:
                for index in range(0, len(encoded), limit):
                    yield tokenizer.decode(encoded[index:index + limit], skip_special_tokens=True)
                pending = ""
            else:
                pending = word
    if pending:
        yield pending


def translate(text, src_lang):
    text = (text or "").strip()
    if not text or src_lang == "en":
        return text
    with _lock:
        translator, tokenizer, target = _load(src_lang)
        clauses = list(_clauses(text, tokenizer))
        sources = [tokenizer.convert_ids_to_tokens(tokenizer.encode(clause)) for clause in clauses]
        results = translator.translate_batch(sources, target_prefix=[[target] for _ in sources],
                                             beam_size=4, max_decoding_length=512, max_batch_size=8)
        translated = []
        for result in results:
            tokens = result.hypotheses[0]
            translated.append(tokenizer.decode(tokenizer.convert_tokens_to_ids(tokens), skip_special_tokens=True).strip())
        return " ".join(part for part in translated if part)
