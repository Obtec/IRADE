"""Rocq (Coq) Jupyter Kernel package powered by ``coqidetop`` STM."""

from __future__ import annotations

__version__ = "0.1.0"

from .driver import RocqDriverError, RocqProcess
from .kernel import RocqKernel
from .session import RocqExecutionOutcome, RocqSession, STMNode, tokenize_sentences

__all__ = [
    "__version__",
    "RocqDriverError",
    "RocqExecutionOutcome",
    "RocqKernel",
    "RocqProcess",
    "RocqSession",
    "STMNode",
    "tokenize_sentences",
]
