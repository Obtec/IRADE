"""CLI installer and diagnostics for the Lean 4 Jupyter kernel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from jupyter_client.kernelspec import KernelSpecManager

from .repl import (
    REPLBinaryManager,
    REPLProcess,
    default_repo_root,
    detect_lean_version,
)

LOGO_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#2563eb"/>
  <text x="32" y="45" font-family="ui-monospace, Menlo, monospace" font-size="38"
        font-weight="700" fill="#ffffff" text-anchor="middle">∀</text>
</svg>
"""


def install_kernelspec(
    kernel_name: str = "lean4",
    display_name: str = "Lean 4",
    user: bool = True,
    prefix: str | None = None,
    build_repl: bool = False,
) -> str:
    """Install the ``lean4`` Jupyter kernelspec and return its destination directory."""
    manager = REPLBinaryManager()
    if build_repl:
        manager.build_in_dir(manager.vendor_dir)

    repo_root = default_repo_root()
    env_vars: dict[str, str] = {}
    if repo_root.is_dir():
        env_vars["LEAN_REPL_REPO"] = str(repo_root)

    kernel_json = {
        "argv": [
            sys.executable,
            "-m",
            "lean4_kernel",
            "-f",
            "{connection_file}",
        ],
        "display_name": display_name,
        "language": "lean4",
        "interrupt_mode": "signal",
        "env": env_vars,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "kernel.json").write_text(
            json.dumps(kernel_json, indent=2) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "logo-svg.svg").write_text(LOGO_SVG, encoding="utf-8")

        ksm = KernelSpecManager()
        dest = ksm.install_kernel_spec(
            str(tmp_path),
            kernel_name=kernel_name,
            user=user if prefix is None else False,
            prefix=prefix,
        )
    return dest


def run_doctor(kernel_name: str = "lean4") -> int:
    """Run health checks on Lean 4, ``repl`` binary, JSON round-trip, and Jupyter kernelspec."""
    print("=== Lean 4 Jupyter Kernel Doctor ===")
    lean_ver = detect_lean_version()
    print(f"[1/4] Lean toolchain     : {lean_ver}")

    mgr = REPLBinaryManager()
    try:
        repl_bin = mgr.resolve_binary(auto_build=False)
        print(f"[2/4] REPL binary        : {repl_bin}")
    except Exception as exc:
        print(f"[2/4] REPL binary        : MISSING ({exc})")
        return 1

    proc = REPLProcess(binary_manager=mgr)
    try:
        res = proc.request({"cmd": "#eval 40 + 2"}, timeout=15.0)
        msgs = res.data.get("messages", [])
        val = msgs[0].get("data") if msgs else None
        ok_eval = val == "42"
        print(f"[3/4] REPL round-trip    : {'OK' if ok_eval else 'UNEXPECTED'} (#eval 40 + 2 => {val!r})")
        if not ok_eval:
            return 1
    except Exception as exc:
        print(f"[3/4] REPL round-trip    : FAILED ({exc})")
        return 1
    finally:
        proc.kill()

    ksm = KernelSpecManager()
    specs = ksm.find_kernel_specs()
    spec_dir = specs.get(kernel_name)
    if spec_dir:
        print(f"[4/4] Jupyter kernelspec : {kernel_name} -> {spec_dir}")
    else:
        print(f"[4/4] Jupyter kernelspec : '{kernel_name}' not installed yet (run `lean4-kernel install`)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lean4-kernel",
        description="Manage the Lean 4 Jupyter kernel (powered by leanprover-community/repl).",
    )
    sub = parser.add_subparsers(dest="subcommand")

    p_inst = sub.add_parser("install", help="Install the Lean 4 Jupyter kernelspec")
    p_inst.add_argument("--name", default="lean4", help="Kernel name (default: lean4)")
    p_inst.add_argument(
        "--display-name",
        default="Lean 4",
        help="Display name in Jupyter UI (default: 'Lean 4')",
    )
    scope = p_inst.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", default=True, help="Install for current user (default)")
    scope.add_argument("--sys-prefix", action="store_true", help="Install into sys.prefix (e.g. active venv/conda)")
    scope.add_argument("--prefix", default=None, help="Install into custom prefix path")
    p_inst.add_argument(
        "--build-repl",
        action="store_true",
        help="Build vendor/repl with `lake build repl` before installing",
    )

    p_build = sub.add_parser("build-repl", help="Build the Lean 4 REPL binary (`lake build repl`)")
    p_build.add_argument(
        "--dir",
        default=None,
        help="Optional path to REPL repository (defaults to vendor/repl)",
    )

    p_doc = sub.add_parser("doctor", help="Check Lean, REPL binary, and Jupyter kernelspec health")
    p_doc.add_argument("--name", default="lean4", help="Kernel name to check (default: lean4)")

    p_uninst = sub.add_parser("uninstall", help="Remove the Lean 4 Jupyter kernelspec")
    p_uninst.add_argument("--name", default="lean4", help="Kernel name to remove (default: lean4)")

    p_serve = sub.add_parser(
        "serve",
        help="Start the Lean 4 Proof Notebook Studio web server integrated with Project-Agora",
    )
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8890, help="Bind port (default: 8890)")
    p_serve.add_argument(
        "--agora-url",
        default=None,
        help="Project-Agora API base URL (default: $AGORA_API_URL or https://127.0.0.1:8444)",
    )
    p_serve.add_argument(
        "--agora-public-url",
        default=None,
        help="Public Project-Agora web URL for browser links",
    )
    p_serve.add_argument(
        "--data-dir",
        default=None,
        help="Directory for storing user Proof Notebooks",
    )
    p_serve.add_argument(
        "--max-kernels",
        type=int,
        default=12,
        help="Maximum concurrent Lean 4 kernels in the LRU pool (default: 12)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cmd = args.subcommand or "install"
    if cmd == "install":
        prefix = getattr(args, "prefix", None)
        if getattr(args, "sys_prefix", False):
            prefix = sys.prefix
        user = prefix is None and getattr(args, "user", True)
        dest = install_kernelspec(
            kernel_name=getattr(args, "name", "lean4"),
            display_name=getattr(args, "display_name", "Lean 4"),
            user=user,
            prefix=prefix,
            build_repl=getattr(args, "build_repl", False),
        )
        print(f"Installed Lean 4 Jupyter kernelspec at: {dest}")
        return 0

    if cmd == "build-repl":
        mgr = REPLBinaryManager()
        target = Path(args.dir).expanduser().resolve() if args.dir else mgr.vendor_dir
        bin_path = mgr.build_in_dir(target)
        print(f"Built Lean 4 REPL binary at: {bin_path}")
        return 0

    if cmd == "doctor":
        return run_doctor(kernel_name=args.name)

    if cmd == "uninstall":
        ksm = KernelSpecManager()
        ksm.remove_kernel_spec(args.name)
        print(f"Removed Jupyter kernelspec '{args.name}'.")
        return 0

    if cmd == "serve":
        from .web import serve

        return serve(
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            agora_api_url=args.agora_url,
            agora_public_url=args.agora_public_url,
            max_kernels=args.max_kernels,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

