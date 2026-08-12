"""Independent multilingual NLI boundary used only as a fail-closed translation risk gate."""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from modules.ai.contracts import AIContractError

MODEL_REVISION = "ca5daf3d11b6c4b3143b1f4602a2edfb64c3ad7e"
MODEL_SHA256 = "95743cad9148e16eac551a13ddccd5cef2bd1e3a56e325451a5e9212fea30cc9"
TOKENIZER_SHA256 = "d0091a328b3441d754e481db5a390d7f3b8dabc6016869fd13ba350d23ddc4cd"
CONTRADICTION_THRESHOLD = 0.70
ENTAILMENT_THRESHOLD = 0.50
MAX_SEQUENCE_LENGTH = 512


@dataclass(frozen=True, slots=True)
class SemanticRiskResult:
    contradiction_probability: float
    entailment_probability: float
    neutral_probability: float

    def __post_init__(self) -> None:
        probabilities = (
            self.contradiction_probability,
            self.entailment_probability,
            self.neutral_probability,
        )
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
            raise ValueError("semantic risk probabilities must be finite and within 0-1")
        if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-3):
            raise ValueError("semantic risk probabilities must sum to one")


class SemanticRiskBoundary(Protocol):
    async def assess(self, source: str, candidate: str) -> SemanticRiskResult: ...


class OnnxSemanticRiskBoundary:
    """Pinned XNLI classifier; a risk signal, never semantic proof."""

    def __init__(self, *, model_dir: Path, timeout_seconds: float = 10.0) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("semantic risk timeout must be finite and within 0-30 seconds")
        self._model_path = model_dir / "onnx" / "model.onnx"
        self._tokenizer_path = model_dir / "tokenizer.json"
        self._timeout_seconds = timeout_seconds
        self._tokenizer: Tokenizer | None = None
        self._session: ort.InferenceSession | None = None
        self._load_lock = asyncio.Lock()

    @staticmethod
    def _verify(path: Path, expected_sha256: str) -> None:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise AIContractError("independent semantic risk boundary is unavailable") from exc
        if digest != expected_sha256:
            raise AIContractError("independent semantic risk boundary integrity check failed")

    async def _ensure_loaded(self) -> tuple[Tokenizer, ort.InferenceSession]:
        if self._tokenizer is not None and self._session is not None:
            return self._tokenizer, self._session
        async with self._load_lock:
            if self._tokenizer is None or self._session is None:
                self._verify(self._model_path, MODEL_SHA256)
                self._verify(self._tokenizer_path, TOKENIZER_SHA256)
                try:
                    tokenizer = await asyncio.to_thread(
                        Tokenizer.from_file, str(self._tokenizer_path)
                    )
                    session = await asyncio.to_thread(
                        ort.InferenceSession,
                        str(self._model_path),
                        providers=["CPUExecutionProvider"],
                    )
                except Exception as exc:
                    raise AIContractError(
                        "independent semantic risk boundary failed to initialize"
                    ) from exc
                self._tokenizer = tokenizer
                self._session = session
        return self._tokenizer, self._session

    @staticmethod
    def _infer(
        tokenizer: Tokenizer,
        session: ort.InferenceSession,
        source: str,
        candidate: str,
    ) -> SemanticRiskResult:
        encoded = tokenizer.encode(source, candidate)
        if len(encoded.ids) > MAX_SEQUENCE_LENGTH:
            raise AIContractError("translation is too long for independent semantic risk analysis")
        feeds = {
            "input_ids": np.asarray([encoded.ids], dtype=np.int64),
            "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
        }
        try:
            output = cast(np.ndarray, session.run(None, feeds)[0])
            logits = output[0]
        except Exception as exc:
            raise AIContractError("independent semantic risk inference failed") from exc
        shifted = logits - np.max(logits)
        probabilities = np.exp(shifted) / np.exp(shifted).sum()
        return SemanticRiskResult(
            entailment_probability=float(probabilities[0]),
            neutral_probability=float(probabilities[1]),
            contradiction_probability=float(probabilities[2]),
        )

    async def assess(self, source: str, candidate: str) -> SemanticRiskResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                tokenizer, session = await self._ensure_loaded()
                return await asyncio.to_thread(self._infer, tokenizer, session, source, candidate)
        except TimeoutError as exc:
            raise AIContractError("independent semantic risk boundary timed out") from exc


def enforce_semantic_risk(result: SemanticRiskResult) -> None:
    if result.contradiction_probability >= CONTRADICTION_THRESHOLD:
        raise AIContractError("independent semantic contradiction risk is too high")
    if result.entailment_probability < ENTAILMENT_THRESHOLD:
        raise AIContractError("independent semantic confidence is too low")
