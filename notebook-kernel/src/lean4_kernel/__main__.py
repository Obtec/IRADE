"""Entry point for ``python -m lean4_kernel``."""

from __future__ import annotations

from ipykernel.kernelapp import IPKernelApp

from .kernel import Lean4Kernel


def main() -> None:
    IPKernelApp.launch_instance(kernel_class=Lean4Kernel)


if __name__ == "__main__":
    main()

