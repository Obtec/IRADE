# Proof Notebook Test Suite (`tests`)

The `tests` directory contains the automated verification suite for the Lean 4 Jupyter Kernel. It covers REPL JSON stdio protocol framing, stateful environment DAG branching and auto-backtracking, Server-Side Rendered (SSR) InfoView HTML generation, Unicode/keyword completions, and end-to-end headless Jupyter notebook execution via `nbclient`.

---

## 1. Test Architecture & File Manifest

| Test File | Test Targets | Execution Requirements |
| :--- | :--- | :--- |
| `test_repl_protocol.py` | JSON parser framing, stdout buffering, comment stripping, declaration extraction, redeclared symbol regexes, SSR InfoView, mock REPL offline session | Offline (pure Python; no Lean required) |
| `test_session_and_magics.py` | Live `LeanSession` commands, tactic execution, auto-backtracking, `.olean` pickling/unpickling, crash recovery, completion | Requires Lean 4 toolchain (`lean`, `lake`) |
| `test_notebook_e2e.py` | End-to-end headless execution of a multi-cell Lean 4 notebook via `nbclient.NotebookClient`, verifying `#eval`, induction proof, `#print axioms`, and `%env` | Requires Lean 4 toolchain + installed `lean4` kernelspec |
| `test_rocq_protocol.py` | Rocq sentence tokenization (nested comments, strings, dot terminators), XML protocol encoding/decoding, mock driver session & SSR InfoView | Offline (pure Python; no Rocq required) |
| `test_rocq_session.py` | Live `RocqSession` vernacular definitions, tactic proofs, `Edit_at` error rollback, auto-backtracking, Unicode completion, and `%load` | Requires Rocq/Coq toolchain (`coqidetop`) |
| `test_web_platform.py` | Web Studio REST endpoints, Redis HOT/COLD lifecycle, dual kernel execution pool, presence, and notebook export | Offline/In-memory (mocked Redis & HTTP server) |

---

## 2. Test Cases Breakdown

### 2.1 `test_repl_protocol.py` (Unit & Protocol Verification)
- **`test_parse_repl_json_multiline`**: Verifies multi-line JSON blocks emitted by REPL before blank lines are parsed correctly into dictionaries.
- **`test_parse_repl_json_with_prefix_stdout`**: Ensures raw C stdout printed before JSON is preserved in `aux_stdout`.
- **`test_strip_comments_and_import_detection`**: Tests parsing through nested block comments (`/- /- ... -/ -/`), line comments (`--`), and module header detection.
- **`test_extract_declared_names`**: Verifies regex extraction of top-level identifiers across `def`, `theorem`, `instance`, `structure`, `noncomputable`, etc.
- **`test_redeclared_identifier_extraction_unicode_quotes`**: Tests regex parsing of Lean 4 redeclaration errors across various quote marks (`'name'`, `` `name` ``, `‘name’`, `“name”`).
- **`test_format_tactic_input_single_and_multiline`**: Validates wrapping multi-line tactic scripts in `(\n  ...\n)` for `tacticSeq`.
- **`test_extract_version_tag`**: Tests toolchain version tag extraction from git and elan version strings.
- **`test_infoview_ssr_fallback_without_js`**: Verifies that the right-hand InfoView pane is pre-rendered via Server-Side Rendering (SSR) for static notebook exports and no-JS clients.
- **`test_offline_lean_session_with_mock_repl`**: Uses a lightweight mock REPL subprocess to verify environment branching, rollback on error, and tactic execution without needing Lean 4 installed.
- **`test_repl_process_timeout_and_crash_with_mock_script`**: Validates process killing and automatic restart upon encountering timeouts or subprocess crashes.

### 2.2 `test_session_and_magics.py` (Session & Magics Integration)
- **`test_unicode_and_keyword_completion`**: Verifies `<Tab>` symbol replacement (`\forall` $\to \forall$, `\le` $\to \le$, `\.` $\to \cdot$) and keyword/magic completion.
- **`test_is_complete_checks`**: Tests Lean 4 syntax completeness heuristics (brackets, comments, strings, block terminators).
- **`test_live_session_commands_tactics_backtrack_pickle_and_recovery`**: Comprehensive integration test against the real Lean 4 REPL:
  1. Declaration & `#eval` calculation.
  2. Error rollback (failed declaration does not advance `env`).
  3. Auto-backtracking when redefining a function (`def sq ...`).
  4. Non-mutating symbol inspection (`#check`).
  5. Multi-line tactic sequence execution (`imp_trans_demo`).
  6. `.olean` environment pickling and unpickling round-trip.
  7. File loading (`%load`).
  8. REPL process termination and automatic lineage recovery.
  9. Interactive Split-Pane InfoView payload extraction.

### 2.3 `test_notebook_e2e.py` (Full Stack Jupyter E2E)
- **`test_execute_notebook_end_to_end`**: Spawns an `nbclient.NotebookClient` against the installed `lean4` kernel, runs an induction proof cell for Gauss sum ($\sum_{k=0}^n k$), and asserts that `#print axioms` verifies zero unproven axioms (`sorryAx`).

### 2.4 `test_rocq_protocol.py` (Rocq Protocol & Mock Driver)
- **`test_tokenize_sentences_comments_strings_and_dots`**: Sentence tokenization edge cases with nested comments, string periods, and bullet splits.
- **`test_extract_declared_name`**: Vernacular symbol extraction from `Definition`, `Lemma`, `Theorem`, and `Fixpoint`.
- **`test_xml_builders_and_parsers`**: Validates encoding of XML `<call>` commands and recursive decoding of `<goals>`, `<hyp>`, and `<richpp>`.
- **`test_rocq_completeness_heuristics`**: Verifies bracket, quote, and sentence completeness checks.
- **`test_mock_coqidetop_session_and_infoview_ssr`**: Verifies STM state tracking, tactic execution, error rollback, and InfoView SSR rendering via mock subprocess.
- **`test_driver_timeout_and_crash_handling`**: Validates timeout handling and process crash detection.

### 2.5 `test_rocq_session.py` (Live Rocq Integration)
- **`test_rocq_unicode_and_keyword_completion`**: Tab symbol expansion and tactic completion in `RocqKernel`.
- **`test_live_rocq_session_definitions_proofs_rollback_and_backtrack`**: Live `coqidetop` test: definitions, `#check`, inductive proof with `induction`, invalid tactic rollback, proof completion, and `%load`.

### 2.6 `test_web_platform.py` (Web Studio Architecture)
- Tests workspace REST lifecycle (`/api/workspaces`), Redis session mounting, kernel pool dispatching, presence tracking, and notebook serialization.

---

## 3. Running the Test Suite

```bash
# Run all tests (offline unit tests will run; live tests run if Lean 4/Rocq are installed)
.venv/bin/pytest

# Run Lean 4 offline and live tests
.venv/bin/pytest tests/test_repl_protocol.py tests/test_session_and_magics.py

# Run Rocq offline and live tests
.venv/bin/pytest tests/test_rocq_protocol.py tests/test_rocq_session.py

# Run Web Studio platform tests
.venv/bin/pytest tests/test_web_platform.py
```

# Run headless notebook E2E test
.venv/bin/pytest -v tests/test_notebook_e2e.py
```
