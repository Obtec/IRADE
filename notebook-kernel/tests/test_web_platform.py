"""End-to-end tests for the Lean 4 + Python Proof Workspace Studio, Redis Lifecycle & Project-Agora Bridge."""

from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import MagicMock, patch

import pytest

from lean4_kernel.web import (
    AgoraRedisClient,
    NotebookStore,
    ProofNotebookHTTPServer,
    create_server,
)

httpx = pytest.importorskip("httpx")


@pytest.fixture()
def web_server(tmp_path: Path):
    redis_client = AgoraRedisClient(force_memory=True)
    server = create_server(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        max_kernels=6,
        redis_client=redis_client,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_notebook_store_starts_empty_without_example_and_supports_lean_python_md(tmp_path: Path) -> None:
    store = NotebookStore(data_dir=tmp_path / "store")
    # Starts with no pre-populated example workspace
    nbs = store.list_notebooks("researcher-1")
    assert nbs == []

    # Create a custom workspace with Markdown, Lean 4 proof, and Python code cells
    created = store.create_notebook(
        "researcher-1",
        title="Group Theory & Python Experiments",
        cells=[
            {
                "id": "md-1",
                "cellType": "markdown",
                "source": "# Monoid Identity Note\nWe prove $x + 0 = x$ and test in Python.",
            },
            {
                "id": "lean-1",
                "cellType": "code",
                "language": "lean4",
                "source": "theorem add_zero_right (x : Nat) : x + 0 = x := by\n  simp",
            },
            {
                "id": "py-1",
                "cellType": "code",
                "language": "python",
                "source": "print([x + 0 == x for x in range(5)])",
            },
        ],
    )
    assert created["title"] == "Group Theory & Python Experiments"
    assert len(created["cells"]) == 3
    assert created["cells"][0]["cellType"] == "markdown"
    assert created["cells"][1]["language"] == "lean4"
    assert created["cells"][2]["language"] == "python"

    listed = store.list_notebooks("researcher-1")
    assert len(listed) == 1
    assert listed[0]["leanCellCount"] == 1
    assert listed[0]["pythonCellCount"] == 1
    assert listed[0]["markdownCellCount"] == 1

    # Export to .ipynb and re-import preserving cell languages
    ipynb = store.to_ipynb(created)
    assert ipynb["nbformat"] == 4
    assert len(ipynb["cells"]) == 3
    assert ipynb["cells"][1]["metadata"]["language"] == "lean4"
    assert ipynb["cells"][2]["metadata"]["language"] == "python"

    reimported = store.from_ipynb(ipynb, title="Reimported Workspace")
    assert reimported["title"] == "Reimported Workspace"
    assert len(reimported["cells"]) == 3
    assert reimported["cells"][0]["cellType"] == "markdown"
    assert reimported["cells"][1]["language"] == "lean4"
    assert reimported["cells"][2]["language"] == "python"

    # Export to .lean source
    lean_src = store.to_lean_source(created)
    assert "/-!" in lean_src
    assert "Monoid Identity Note" in lean_src
    assert "theorem add_zero_right" in lean_src
    assert "[Python Cell 3]" in lean_src


def test_workspace_redis_lifecycle_mount_update_unmount(
    web_server: tuple[str, ProofNotebookHTTPServer],
) -> None:
    base_url, _ = web_server
    headers = {"X-Proof-User": "researcher-redis", "X-Proof-Client": "tab-1"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20.0) as client:
        # Initially empty workspace list
        r_list0 = client.get("/api/workspaces")
        assert r_list0.status_code == 200
        assert r_list0.json()["workspaces"] == []

        # Create Workspace A and Workspace B (simulating multiple open workspace tabs in VS Code)
        r_ws_a = client.post("/api/workspaces", json={"title": "Workspace Alpha", "language": "lean4"})
        assert r_ws_a.status_code == 201
        ws_a = r_ws_a.json()["workspace"]
        assert r_ws_a.json()["lifecycle"]["state"] == "HOT"
        assert r_ws_a.json()["lifecycle"]["inRedis"] is True

        r_ws_b = client.post("/api/workspaces", json={"title": "Workspace Beta", "language": "python"})
        assert r_ws_b.status_code == 201
        ws_b = r_ws_b.json()["workspace"]
        assert r_ws_b.json()["lifecycle"]["state"] == "HOT"

        # Both workspaces are simultaneously mounted in Redis (HOT)
        r_list1 = client.get("/api/workspaces")
        by_id = {w["id"]: w for w in r_list1.json()["workspaces"]}
        assert by_id[ws_a["id"]]["lifecycle"]["state"] == "HOT"
        assert by_id[ws_b["id"]]["lifecycle"]["state"] == "HOT"

        # Update Workspace A while HOT in Redis
        ws_a["title"] = "Workspace Alpha (Updated in Redis)"
        ws_a["cells"][0]["source"] = "#eval 100 + 23"
        r_put = client.put(f"/api/workspaces/{ws_a['id']}", json={"workspace": ws_a})
        assert r_put.status_code == 200
        assert r_put.json()["lifecycle"]["state"] == "HOT"

        # Close/unmount Workspace A tab -> flushes from Redis to disk and transitions to COLD
        r_unm = client.post(
            f"/api/workspaces/{ws_a['id']}/unmount",
            json={"clientId": "tab-1", "force": True},
        )
        assert r_unm.status_code == 200
        unm_data = r_unm.json()
        assert unm_data["unmounted"] is True
        assert unm_data["lifecycle"]["state"] == "COLD"
        assert unm_data["lifecycle"]["inRedis"] is False
        assert unm_data["lifecycle"]["flushedToDisk"] is True

        # Verify Workspace A is COLD while Workspace B remains HOT
        r_list2 = client.get("/api/workspaces")
        by_id2 = {w["id"]: w for w in r_list2.json()["workspaces"]}
        assert by_id2[ws_a["id"]]["lifecycle"]["state"] == "COLD"
        assert by_id2[ws_b["id"]]["lifecycle"]["state"] == "HOT"

        # Re-open/mount Workspace A -> transitions COLD -> HOT with incremented cacheGeneration and preserves edits
        r_remount = client.post(f"/api/workspaces/{ws_a['id']}/mount", json={"clientId": "tab-1"})
        assert r_remount.status_code == 200
        remounted = r_remount.json()
        assert remounted["lifecycle"]["state"] == "HOT"
        assert remounted["lifecycle"]["cacheGeneration"] >= 2
        assert remounted["workspace"]["title"] == "Workspace Alpha (Updated in Redis)"
        assert remounted["workspace"]["cells"][0]["source"] == "#eval 100 + 23"


def test_web_static_and_metadata_endpoints(web_server: tuple[str, ProofNotebookHTTPServer]) -> None:
    base_url, _ = web_server
    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        r_index = client.get("/")
        assert r_index.status_code == 200
        assert "Lean 4 Proof Studio" in r_index.text
        assert "btn-add-python-cell" in r_index.text

        r_js = client.get("/static/app.js")
        assert r_js.status_code == 200
        assert "openOrSwitchWorkspace" in r_js.text
        assert "closeWorkspaceTab" in r_js.text

        r_css = client.get("/static/styles.css")
        assert r_css.status_code == 200
        assert ".vscode-welcome-screen" in r_css.text

        r_uni = client.get("/api/unicode")
        assert r_uni.status_code == 200
        uni_data = r_uni.json()
        assert uni_data["abbreviations"][r"\forall"] == "∀"
        assert "%env" in uni_data["magics"]

        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        health_data = r_health.json()
        assert health_data["status"] == "UP"
        assert "agora" in health_data
        assert "redis" in health_data


def test_web_dual_kernel_lean4_proof_and_python_code_execution(
    web_server: tuple[str, ProofNotebookHTTPServer],
) -> None:
    base_url, _ = web_server
    headers = {"X-Proof-User": "alice-polyglot"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=60.0) as client:
        # 1. Create a workspace with Lean 4 definition, Lean 4 sumUpTo_double proof (omega), and Python 3 cell
        r_create = client.post(
            "/api/workspaces",
            json={
                "title": "Alice Lean 4 + Python Workspace",
                "cells": [
                    {
                        "id": "c-def",
                        "cellType": "code",
                        "language": "lean4",
                        "source": (
                            "def sumUpTo : Nat → Nat\n"
                            "  | 0 => 0\n"
                            "  | n + 1 => (n + 1) + sumUpTo n\n\n"
                            "#eval sumUpTo 10"
                        ),
                    },
                    {
                        "id": "c-thm",
                        "cellType": "code",
                        "language": "lean4",
                        "source": (
                            "theorem sumUpTo_double (n : Nat) : 2 * sumUpTo n = n * (n + 1) := by\n"
                            "  induction n with\n"
                            "  | zero =>\n"
                            "    simp [sumUpTo]\n"
                            "  | succ k ih =>\n"
                            "    simp [sumUpTo, Nat.mul_add, Nat.add_mul] at *\n"
                            "    omega"
                        ),
                    },
                    {
                        "id": "c-py",
                        "cellType": "code",
                        "language": "python",
                        "source": (
                            "def sum_up_to(n):\n"
                            "    return sum(range(n + 1))\n"
                            "print('PYTHON_CHECK:', [2 * sum_up_to(k) == k * (k + 1) for k in range(6)])"
                        ),
                    },
                ],
            },
        )
        assert r_create.status_code == 201
        ws = r_create.json()["workspace"]
        ws_id = ws["id"]

        # 2. Execute Lean 4 cell 1 (definition + #eval sumUpTo 10 == 55)
        r_exec1 = client.post(
            f"/api/workspaces/{ws_id}/execute",
            json={"cellId": "c-def", "language": "lean4", "code": ws["cells"][0]["source"]},
        )
        assert r_exec1.status_code == 200
        exec1 = r_exec1.json()
        assert exec1["exitCode"] == 0
        assert exec1["language"] == "lean4"
        html_outputs_1 = [
            o["data"].get("text/html", "")
            for o in exec1["outputs"]
            if o["output_type"] in ("display_data", "execute_result")
        ]
        assert any("55" in h for h in html_outputs_1)

        # 3. Execute Lean 4 cell 2 (sumUpTo_double induction + omega proof)
        r_exec2 = client.post(
            f"/api/workspaces/{ws_id}/execute",
            json={"cellId": "c-thm", "language": "lean4", "code": ws["cells"][1]["source"]},
        )
        assert r_exec2.status_code == 200
        exec2 = r_exec2.json()
        assert exec2["exitCode"] == 0
        html_outputs_2 = [
            o["data"].get("text/html", "")
            for o in exec2["outputs"]
            if o["output_type"] in ("display_data", "execute_result")
        ]
        assert any("l4iv-wrap" in h and "Goals accomplished" in h for h in html_outputs_2)

        # 4. Execute Python 3 cell 3 in the same workspace
        r_exec3 = client.post(
            f"/api/workspaces/{ws_id}/execute",
            json={"cellId": "c-py", "language": "python", "code": ws["cells"][2]["source"]},
        )
        assert r_exec3.status_code == 200
        exec3 = r_exec3.json()
        assert exec3["exitCode"] == 0
        assert exec3["language"] == "python"
        stream_texts = [
            o.get("text", "")
            for o in exec3["outputs"]
            if o.get("output_type") == "stream"
        ]
        assert any("PYTHON_CHECK: [True, True, True, True, True, True]" in t for t in stream_texts)

        # 5. Verify omega diagnostic hint appears when user runs unexpanded omega on sumUpTo
        broken_omega_code = (
            "theorem sumUpTo_double_bad (n : Nat) : 2 * sumUpTo n = n * (n + 1) := by\n"
            "  induction n with\n"
            "  | zero => simp [sumUpTo]\n"
            "  | succ k ih =>\n"
            "    simp [sumUpTo, Nat.mul_add]\n"
            "    omega"
        )
        r_exec_bad = client.post(
            f"/api/workspaces/{ws_id}/execute",
            json={"cellId": "c-bad-omega", "language": "lean4", "code": broken_omega_code, "persist": False},
        )
        assert r_exec_bad.status_code == 200
        bad_res = r_exec_bad.json()
        assert bad_res["exitCode"] == 1
        bad_html = "".join(
            o["data"].get("text/html", "")
            for o in bad_res["outputs"]
            if o["output_type"] in ("display_data", "execute_result")
        )
        assert "omega could not prove the goal" in bad_html
        assert "simp [sumUpTo, Nat.mul_add, Nat.add_mul] at *" in bad_html


def test_agora_bridge_canvas_export_and_import(
    web_server: tuple[str, ProofNotebookHTTPServer],
) -> None:
    base_url, server = web_server

    fake_sdk = MagicMock()
    fake_sdk.__enter__.return_value = fake_sdk
    fake_sdk.__exit__.return_value = None
    fake_sdk.create_item.side_effect = [
        {"id": "canvas-nb-item-1", "type": "notebook"},
        {"id": "canvas-summary-item-2", "type": "text"},
    ]
    fake_sdk.get_canvas.return_value = {
        "id": "canvas-uuid-1234",
        "title": "Collaborative Formal Methods Canvas",
        "description": "Shared R&D board",
        "objects": [
            {
                "id": "obj-text-1",
                "type": "text",
                "content": "# Research Hypothesis\nCommutativity of addition.",
                "customData": {"text": "# Research Hypothesis\nCommutativity of addition."},
            },
            {
                "id": "obj-code-1",
                "type": "code",
                "content": "theorem add_comm_sample (a b : Nat) : a + b = b + a := by omega",
                "customData": {"code": "theorem add_comm_sample (a b : Nat) : a + b = b + a := by omega"},
            },
        ],
    }

    with (
        patch.object(server.app.agora, "get_me", return_value={"id": "user-99", "nickname": "DrLean", "tag_number": "0001"}),
        patch.object(server.app.agora, "_sdk_client", return_value=fake_sdk),
    ):
        headers = {"Authorization": "Bearer test-jwt-token-xyz"}
        with httpx.Client(base_url=base_url, headers=headers, timeout=20.0) as client:
            r_sess = client.get("/api/auth/session")
            assert r_sess.status_code == 200
            assert r_sess.json()["user"]["nickname"] == "DrLean"

            r_imp = client.post("/api/agora/canvases/canvas-uuid-1234/import", json={})
            assert r_imp.status_code == 201
            imported_nb = r_imp.json()["notebook"]
            assert imported_nb["linkedCanvasId"] == "canvas-uuid-1234"
            assert len(imported_nb["cells"]) == 2
            assert imported_nb["cells"][0]["cellType"] == "markdown"
            assert imported_nb["cells"][1]["cellType"] == "code"

            r_exp = client.post(
                "/api/agora/canvases/canvas-uuid-1234/export",
                json={"notebookId": imported_nb["id"], "includeSummaryCard": True},
            )
            assert r_exp.status_code == 200
            exp_data = r_exp.json()
            assert exp_data["export"]["notebookItemId"] == "canvas-nb-item-1"
            assert exp_data["notebook"]["linkedCanvasItemId"] == "canvas-nb-item-1"
            assert fake_sdk.create_item.call_count == 2


def test_realtime_multiuser_sync_and_error_visibility(
    web_server: tuple[str, ProofNotebookHTTPServer],
) -> None:
    base_url, server = web_server

    with (
        httpx.Client(
            base_url=base_url,
            headers={"X-Proof-User": "alice", "X-Proof-Client": "client-alice"},
            timeout=30.0,
        ) as alice,
        httpx.Client(
            base_url=base_url,
            headers={"X-Proof-User": "bob", "X-Proof-Client": "client-bob"},
            timeout=30.0,
        ) as bob,
    ):
        r_create = alice.post(
            "/api/workspaces",
            json={
                "title": "Shared Collaborative Workspace",
                "cells": [
                    {
                        "id": "c-bad-proof",
                        "cellType": "code",
                        "language": "lean4",
                        "source": "theorem false_eq (n : Nat) : n + 1 = n := by\n  rfl",
                    }
                ],
            },
        )
        assert r_create.status_code == 201
        nb_id = r_create.json()["workspace"]["id"]

        sub_q = server.app.realtime.subscribe(nb_id)
        try:
            r_sse = bob.get(f"/api/workspaces/{nb_id}/events?clientId=client-bob&user=bob&once=1")
            assert r_sse.status_code == 200
            assert "data:" in r_sse.text
            assert '"type": "connected"' in r_sse.text

            r_pres = bob.post(
                f"/api/workspaces/{nb_id}/presence",
                json={"clientId": "client-bob", "activeCellId": "c-bad-proof"},
            )
            assert r_pres.status_code == 200
            collabs = r_pres.json()["collaborators"]
            assert any(c["userId"] == "bob" and c["activeCellId"] == "c-bad-proof" for c in collabs)

            r_bob_get = bob.get(f"/api/workspaces/{nb_id}")
            assert r_bob_get.status_code == 200
            nb_bob = r_bob_get.json()["workspace"]
            nb_bob["title"] = "Shared Collaborative Workspace (Edited by Bob)"
            r_bob_put = bob.put(f"/api/workspaces/{nb_id}", json={"workspace": nb_bob})
            assert r_bob_put.status_code == 200

            events = []
            while not sub_q.empty():
                events.append(sub_q.get_nowait())
            assert any(e["type"] == "notebook.saved" for e in events)

            r_exec_fail = alice.post(
                f"/api/workspaces/{nb_id}/execute",
                json={"cellId": "c-bad-proof", "code": nb_bob["cells"][0]["source"]},
            )
            assert r_exec_fail.status_code == 200
            fail_data = r_exec_fail.json()
            assert fail_data["exitCode"] == 1
            html_outs = [
                o["data"].get("text/html", "")
                for o in fail_data["outputs"]
                if o["output_type"] in ("display_data", "execute_result")
            ]
            assert any("l4iv-sec-MsgErr" in h and "var(--jp-ui-font-color1, inherit)" in h for h in html_outs)

            exec_events = []
            while not sub_q.empty():
                exec_events.append(sub_q.get_nowait())
            assert any(e["type"] == "cell.executing" for e in exec_events)
            assert any(e["type"] == "cell.executed" and e["data"]["lastRunStatus"] == "error" for e in exec_events)
        finally:
            server.app.realtime.unsubscribe(nb_id, sub_q)
