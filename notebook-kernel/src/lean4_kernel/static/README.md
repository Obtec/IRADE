# Lean 4 Proof Notebook Studio — Frontend Web Assets (`src/lean4_kernel/static`)

This directory houses the static front-end assets for the **Visual Studio Code R&D Workbench** web interface of the Lean 4 Proof Notebook Studio. It provides an in-browser proof engineering studio featuring interactive cell execution, real-time split-pane InfoViews, CodeMirror cursor synchronization, and shared workspace integration.

---

## 1. Asset Structure

```text
src/lean4_kernel/static/
├── index.html       # Single-Page Application (SPA) HTML5 workbench layout
├── styles.css       # VS Code dark-themed stylesheet using --vscode-* variables
└── app.js           # Client application controller, REST API client, and event bindings
```

---

## 2. Core Frontend Subsystems

### 2.1 Workbench Layout (`index.html`)
- **VS Code Title Bar**: Menus (`File`, `Workspace`, `View`, `Run`, `Kernel`, `Workspace`), Command Center title input with dirty indicator, Redis HOT/COLD lifecycle badge, Live Sync status pill, and workspace deep-link.
- **Activity Bar (48px left rail)**: Icon buttons for Explorer (`workspaces`), Workspace Bridge, and Lean 4 / Rocq Unicode Symbols & Magic command reference palette.
- **Primary Sidebar**: Collapsible sections for:
  - `Open Workspaces (Redis HOT)`: Active tabs mounted in the Redis state machine.
  - `Persistent Workspaces (Disk)`: Local workspace directory file listings.
  - `Shared Workspaces`: Real-time synced remote canvas objects.
- **Editor Area**: Tab bar supporting multiple workspaces, sticky cell toolbar (`+ Lean 4`, `+ Rocq`, `+ Python`, `+ Markdown`), language dropdown (`∀ Lean 4`, `🐓 Rocq`, `🐍 Python 3`, `¶ Markdown`), and interactive cell containers.
- **Split-Pane Proof View**: Dual-column code and proof state inspector with responsive breakpoint at 720px width.

### 2.2 Visual Theming & Design System (`styles.css` & `infoview.py`)
- Faithful reproduction of VS Code Dark+ theme variables (`--vscode-editor-background: #1e1e1e`, `--vscode-activityBar-background: #181818`, `--vscode-sideBar-background: #252526`).
- Native typography using system monospace stacks (`ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`).
- Workbench error cards in `styles.css`: `.l4iv-sec-MsgErr` (`#f87171`) for elaboration errors with custom actionable hints.
- Inline InfoView goal styling (dynamically injected by `infoview.py`):
  - `.l4iv-turnstile` (`#a78bfa`): Turnstile operator ($\vdash$).
  - `.l4iv-case-tag` (`#60a5fa`): Inductive proof branch cases (`case zero`, `case succ`).
  - `.l4iv-sec-Done` (`#34d399`): Resolved proof branches (`✓ Goals accomplished`).

### 2.3 Application Logic & Communication (`app.js`)
- **Lifecycle Management**:
  - `GET /api/workspaces`: Lists open (HOT) and persisted (COLD) workspaces.
  - `POST /api/workspaces`: Creates new workspaces or opens existing ones (mounting into Redis).
  - `POST /api/workspaces/<id>/mount`: Mounts workspace into active Redis session.
  - `POST /api/workspaces/<id>/unmount`: Unmounts workspace from HOT memory to disk.
  - `DELETE /api/workspaces/<id>`: Deletes workspace buffer and frees associated kernel instances.
- **Multi-Language Cell Execution**:
  - `POST /api/workspaces/<id>/execute`: Sends JSON payload `{ cell_id, code, language, timeout }` to backend `ProofKernelPool` (routing to `lean4`, `rocq`, or `python`).
  - `POST /api/workspaces/<id>/complete`: Fast Unicode and kernel completion.
  - `POST /api/workspaces/<id>/inspect`: Hover inspection (`#check` / `Check`).
  - `POST /api/workspaces/<id>/interrupt`: Interrupts currently running kernels.
  - `POST /api/workspaces/<id>/reset`: Resets active kernels to clean environment.
  - Parses Jupyter display MIME bundles (`text/html`, `text/plain`, ANSI traceback error cards).
- **Realtime Collaboration & File I/O**:
  - `GET /api/workspaces/<id>/events`: Server-Sent Events (SSE) stream for live multi-user sync.
  - `POST /api/workspaces/<id>/presence`: Realtime collaborator presence heartbeat.
  - `POST /api/workspaces/import-ipynb`: Imports existing `.ipynb` files into workspace.
  - `GET /api/workspaces/<id>/export.ipynb` / `export.lean`: Exports notebook to `.ipynb` or `.lean` source files.
- **Editor Cursor Synchronization**:
  - Listens to `click`, `keyup`, and `select` events on cell textareas and CodeMirror editors.
  - Dynamically calculates 1-based line and 0-based column positions and instructs the InfoView widget to highlight the active tactic step and term type.
- **Unicode Tab Expansion**:
  - Intercepts `<Tab>` keypresses following backslash sequences (e.g. `\forall<Tab>` $\to \forall$, `\to<Tab>` $\to \to$, `\<` $\to \langle$).

---

## 3. Boundary & Security Rules

1. **Zero External CDN Dependencies**: All assets are locally bundled and served by `ProofNotebookHTTPServer`.
2. **HTML Sanitization**: Outputs rendered from Markdown cells and kernel outputs are escaped or pre-processed through structured DOM nodes to prevent cross-site scripting (XSS).
3. **Relative API Routing**: All REST calls are relative (`/api/...`), enabling transparent reverse-proxy deployment behind Nginx, Docker, or Kubernetes.
