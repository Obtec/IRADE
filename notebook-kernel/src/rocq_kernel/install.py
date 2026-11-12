"""CLI installer and diagnostics for the Rocq (Coq) Jupyter kernel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from jupyter_client.kernelspec import KernelSpecManager

from .driver import (
    RocqBinaryNotFoundError,
    RocqProcess,
    detect_rocq_version,
    find_coqidetop_binary,
)
from .xml_protocol import build_query_call

ROCQ_LOGO_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#d97706"/>
  <text x="32" y="45" font-family="ui-monospace, Menlo, monospace" font-size="38"
        font-weight="700" fill="#ffffff" text-anchor="middle">⊢</text>
</svg>
"""


def install_kernelspec(
    kernel_name: str = "rocq",
    display_name: str = "Rocq (Coq)",
    user: bool = True,
    prefix: str | None = None,
) -> str:
    """Install the ``rocq`` Jupyter kernelspec and return its destination directory."""
    kernel_json = {
        "argv": [
            sys.executable,
            "-m",
            "rocq_kernel",
            "-f",
            "{connection_file}",
        ],
        "display_name": display_name,
        "language": "coq",
        "interrupt_mode": "signal",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "kernel.json").write_text(
            json.dumps(kernel_json, indent=2) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "logo-svg.svg").write_text(ROCQ_LOGO_SVG, encoding="utf-8")

        ksm = KernelSpecManager()
        dest = ksm.install_kernel_spec(
            str(tmp_path),
            kernel_name=kernel_name,
            user=user if prefix is None else False,
            prefix=prefix,
        )
    return dest


def run_doctor(kernel_name: str = "rocq") -> int:
    """Run health checks on Rocq/Coq ``coqidetop``, XML round-trip, and Jupyter kernelspec."""
    print("=== Rocq (Coq) Jupyter Kernel Doctor ===")
    rocq_ver = detect_rocq_version()
    print(f"[1/4] Rocq toolchain     : {rocq_ver}")

    try:
        bin_path = find_coqidetop_binary()
        print(f"[2/4] coqidetop binary   : {bin_path}")
    except RocqBinaryNotFoundError as exc:
        print(f"[2/4] coqidetop binary   : MISSING ({exc})")
        return 1

    proc = RocqProcess(binary_path=bin_path)
    try:
        root_id = proc.start()
        qres = proc.send_call(build_query_call("Check nat.", root_id), timeout=10.0)
        ok_query = qres.ok and any("Set" in fb.message for fb in qres.feedbacks)
        print(f"[3/4] STM XML round-trip : {'OK' if ok_query else 'UNEXPECTED'} (Init => state #{root_id})")
        if not ok_query:
            return 1
    except Exception as exc:
        print(f"[3/4] STM XML round-trip : FAILED ({exc})")
        return 1
    finally:
        proc.kill()

    spec_dir = None
    try:
        ksm = KernelSpecManager()
        specs = ksm.find_kernel_specs()
        spec_dir = specs.get(kernel_name)
    except Exception:
        sys_spec = Path(sys.prefix) / "share" / "jupyter" / "kernels" / kernel_name
        if sys_spec.is_dir():
            spec_dir = str(sys_spec)
    if spec_dir:
        print(f"[4/4] Jupyter kernelspec : {kernel_name} -> {spec_dir}")
    else:
        print(f"[4/4] Jupyter kernelspec : '{kernel_name}' not installed yet (run `rocq-kernel install`)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rocq-kernel",
        description="Manage the Rocq (Coq) Jupyter kernel (powered by coqidetop STM).",
    )
    sub = parser.add_subparsers(dest="subcommand")

    p_inst = sub.add_parser("install", help="Install the Rocq (Coq) Jupyter kernelspec")
    p_inst.add_argument("--name", default="rocq", help="Kernel name (default: rocq)")
    p_inst.add_argument(
        "--display-name",
        default="Rocq (Coq)",
        help="Display name in Jupyter UI (default: 'Rocq (Coq)')",
    )
    scope = p_inst.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", default=True, help="Install for current user (default)")
    scope.add_argument("--sys-prefix", action="store_true", help="Install into sys.prefix")
    scope.add_argument("--prefix", default=None, help="Install into custom prefix path")

    p_doc = sub.add_parser("doctor", help="Check Rocq/Coq binary, STM XML handshake, and kernelspec health")
    p_doc.add_argument("--name", default="rocq", help="Kernel name to check (default: rocq)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "install":
        prefix = sys.prefix if args.sys_prefix else args.prefix
        dest = install_kernelspec(
            kernel_name=args.name,
            display_name=args.display_name,
            user=args.user and prefix is None,
            prefix=prefix,
        )
        print(f"Installed Jupyter kernelspec '{args.name}' ({args.display_name}) at:\n  {dest}")
        return 0

    if args.subcommand == "doctor":
        return run_doctor(kernel_name=args.name)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
