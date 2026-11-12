"""Entrypoint for launching the Rocq (Coq) Jupyter kernel via ``python -m rocq_kernel``."""

from __future__ import annotations

from ipykernel.kernelapp import IPKernelApp

from .kernel import RocqKernel

if __name__ == "__main__":
    IPKernelApp.launch_instance(kernel_class=RocqKernel)
