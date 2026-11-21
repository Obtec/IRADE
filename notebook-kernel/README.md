# proof-notebook — Interactive Theorem Proving Notebooks for Lean 4 & Rocq (Coq)

`proof-notebook`은 **Lean 4**와 **Rocq (Coq)** 형식 증명 및 대화형 전술(Tactic) 탐색을 Jupyter Notebook / JupyterLab / VS Code Notebook 및 전용 웹 기반 Proof Studio에서 수행할 수 있는 대화형 증명 환경입니다.

- **Lean 4 커널**: [`leanprover-community/repl`](https://github.com/leanprover-community/repl)을 백엔드로 사용하여 상주형 REPL 프로세스, 불변 환경 스냅샷 그래프, InfoTree 파싱 기반 Split-Pane InfoView를 제공합니다.
- **Rocq (Coq) 커널**: `coqidetop` XML stdio 프로토콜과 STM(State Transaction Manager) 상태 트리를 기반으로 문장 단위 실행, 자동 백트래킹(`Edit_at`), 전술 목표(`⊢`) 및 가설 검사를 지원합니다.
- **Web Proof Studio**: Lean 4, Rocq, Python 3 다중 증명기 커널 풀(`ProofKernelPool`)과 에디터 커서 연동 InfoView를 내장한 웹 프론트엔드를 제공합니다.

---

## 주요 기능 (Key Features)

### 1. Lean 4 Jupyter Kernel (`lean4_kernel`)
- **초고속 상주형 REPL 세션 (`REPLProcess`)**: `leanprover-community/repl` 프로세스를 백그라운드에 상주시키고 JSON over stdio(빈 줄 구분 프레이밍)로 통신합니다. `LEAN_SYSROOT`를 최초 1회 해석하여 셀 실행마다 프로세스가 재시작되는 오버헤드를 방지합니다.
- **불변 환경 스냅샷 그래프 & 스마트 재정의 분기 (`Auto-Backtrack`)**: 셀 오류 시 이전 정상 `env` 스냅샷을 보존하며, 이미 선언된 식별자를 재실행하면 이전 조상 스냅샷에서 자동으로 새 브랜치를 생성합니다.
- **대화형 Lean 4 InfoView (Split-Pane 위젯 + 커서 연동)**: 증명 셀 실행 시 `REPL`의 `InfoTree`(`TacticInfo` + `TermInfo`)를 파싱하여 코드와 InfoView(`Goals Before`, `Goals After`, `Term at Cursor`)를 분할 렌더링하고, 에디터 커서 이동 시 실시간으로 동기화됩니다.
- **멀티라인 대화형 전술 모드 (`%%tactic`, `%tactic`)**: 열린 목표(`⊢ ...`)가 있는 상태에서 셀 단위로 단계별 전술을 적용하고 목표 해결 여부를 확인합니다.
- **Lake / Mathlib 프로젝트 연동**: `lakefile.toml` 또는 `lakefile.lean` 환경에서 `lake env repl` 모드로 자동 구동되어 로컬 모듈 및 Mathlib `import`를 지원합니다.

### 2. Rocq (Coq) Jupyter Kernel (`rocq_kernel`)
- **`coqidetop` XML stdio 프로토콜 & STM 연동**: Rocq / Coq의 대화형 백엔드인 `coqidetop` 프로세스를 비동기 구동하고 XML 통신(Add, Edit_at, Query, Goal)을 수행합니다. Coq 8.14+ 및 Rocq 9.3의 `<state_id val="..."/>` 스키마를 준수합니다.
- **STM 상태 백트래킹 & 셀 트랜잭션 격리**: 노트북 셀 코드를 문장 단위(`Sentence`)로 분할 실행하며, 실행 실패 시 셀 진입 전 STM 상태로 자동 롤백(`Edit_at`)하여 세션 상태 오염을 방지합니다.
- **대화형 Rocq InfoView 위젯**: 현재 열려 있는 서브골(`⊢`)과 가설 목록(`x : nat`, `H : ...`)을 Split-Pane 카드 형태로 렌더링하며, 증명이 완료되면 `✓ All goals completed!` 배지를 표시합니다.
- **편집기 편의 기능**: 백슬래시 유니코드 기호 자동완성(`\forall` → `∀`, `\to` → `→`, `\exists` → `∃`), 키워드/전술 자동완성, `Shift+Tab` 식별자 검사(`Check <id>.`), 문장 완결성 자동 검사(마침표 `.` 및 괄호/주석 중첩 감지)를 제공합니다.

### 3. Proof Notebook Studio & 다중 커널 플랫폼 (`web.py` + `app.js`)
- **다중 증명기 지원 (`ProofKernelPool`)**: 단일 웹 인터페이스에서 Lean 4, Rocq (Coq), Python 3 노트북 커널 인스턴스를 격리 관리하고 동시 실행합니다.
- **실시간 반응형 웹 에디터**: 브라우저 상에서 분할 뷰로 코드 작성, 셀 단위 실행, 커서 연동 InfoView 조회, 마크다운/수식 렌더링을 제공합니다.
- **공유 Workspace 브리지**: 인증 및 작업공간 마운트 API를 통해 검증된 증명 노트북을 협업 캔버스 객체로 공유하거나 가져올 수 있습니다.

---

## 프로젝트 구조

```text
proof-notebook/
├── vendor/repl/                 # leanprover-community/repl (git submodule)
├── src/lean4_kernel/            # Lean 4 Jupyter 커널 및 Web Studio 서버
│   ├── repl.py                  # REPL 바이너리 관리 및 JSON stdio 프로토콜 클라이언트
│   ├── session.py               # 불변 환경(env)·증명상태(proofState) 그래프 및 자동 분기/복구
│   ├── infoview.py              # InfoTree 파서 및 Split-Pane + 에디터 커서 연동 InfoView 위젯
│   ├── magics.py                # % / %% 매직 커맨드 파서 및 디스패처
│   ├── renderer.py              # 다크/라이트 테마 대응 HTML 및 텍스트 출력 렌더러
│   ├── unicode_map.py           # Lean 4 백슬래시 유니코드 기호 및 키워드/전술 사전
│   ├── kernel.py                # ipykernel 기반 Lean4Kernel 구현
│   ├── web.py                   # 다중 증명기 Web Studio 서버 & Workspace 브리지
│   ├── static/                  # 웹 프론트엔드 에셋 (index.html, styles.css, app.js)
│   └── install.py               # `lean4-kernel` CLI (설치, REPL 빌드, 상태 점검, 웹 서버 실행)
├── src/rocq_kernel/             # Rocq (Coq) Jupyter 커널
│   ├── driver.py                # coqidetop 프로세스 관리 및 XML stdio 통신
│   ├── xml_protocol.py          # Coq/Rocq XML 호출 빌더 및 응답 파서
│   ├── session.py               # STM 상태 트리, 문장 파서 및 트랜잭션 관리
│   ├── infoview.py              # Rocq InfoView HTML split-pane 렌더러
│   ├── unicode_map.py           # Coq/Rocq 유니코드 기호, 키워드 및 전술 사전
│   ├── kernel.py                # ipykernel 기반 RocqKernel 구현
│   └── install.py               # `rocq-kernel` CLI (커널스펙 등록 및 doctor 점검)
├── examples/
│   ├── Arithmetic.lean          # Lean 4 %load 테스트용 예제 모듈
│   └── Arithmetic.v             # Rocq (Coq) %load 테스트용 예제 모듈
├── notebooks/
│   ├── lean4_demo.ipynb         # 실행 결과가 포함된 Lean 4 데모 노트북
│   └── rocq_demo.ipynb          # 실행 결과가 포함된 Rocq (Coq) 데모 노트북
└── tests/                       # 프로토콜, 세션/매직, nbclient E2E, 웹 플랫폼 테스트 스위트
```

---

## 설치 및 시작하기 (Quick Start)

### 사전 요구사항
- **Lean 4**: [`elan`](https://github.com/leanprover/elan) (`lean`, `lake` 포함)
- **Rocq (Coq)**: `coqidetop` (Rocq Prover 9.x 또는 Coq 8.14+; `brew install coq` 또는 `opam install coq`)
- **Python**: 3.10 이상

### 1. 저장소 클론 및 가상환경 설치

```bash
git clone --recurse-submodules https://github.com/Obtec/proof-notebook.git
cd proof-notebook

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### 2. Jupyter 커널 등록 및 상태 점검

#### (A) Lean 4 커널 등록
```bash
# vendor/repl 빌드 + 사용자 Jupyter 커널스펙(lean4) 등록
.venv/bin/lean4-kernel install --user --build-repl

# 정상 설치 및 REPL 왕복 통신 점검
.venv/bin/lean4-kernel doctor
```

#### (B) Rocq (Coq) 커널 등록
```bash
# 사용자 Jupyter 커널스펙(rocq) 등록
.venv/bin/rocq-kernel install --user

# coqidetop 바이너리, STM XML 왕복 통신, 커널스펙 점검
.venv/bin/rocq-kernel doctor
```

### 3. 노트북 실행 (JupyterLab 또는 Web Studio)

```bash
# (A) 웹 기반 다중 증명기 Web Studio 실행 (HTTPS 전용, 기본 포트: 8890)
.venv/bin/lean4-kernel serve --host 127.0.0.1 --port 8890 --self-signed
# 또는 Let's Encrypt/내부 TLS 인증서 지정:
# .venv/bin/lean4-kernel serve --host 127.0.0.1 --port 8890 --tls-dir ~/.local/share/irade/letsencrypt/irade.p-e.kr
# 또는
.venv/bin/lean4-proof-studio

# (B) JupyterLab에서 실행 (Lean 4 및 Rocq 노트북 지원)
jupyter lab
```

- **웹 스튜디오 (`https://127.0.0.1:8890`)**:
  - `ProofKernelPool`을 통해 **Lean 4**, **Rocq (Coq)**, **Python 3** 커널 인스턴스를 독립적으로 관리합니다.
  - 상단 툴바의 언어 선택기 또는 `+ Lean 4`, `+ Rocq`, `+ Code`, `+ Markdown` 버튼으로 셀을 추가하고 실행할 수 있습니다.
  - 증명 셀 실행 시 우측 또는 하단에 전용 **InfoView 패널**이 열려 현재 목표와 가설을 실시간으로 확인할 수 있습니다.

---

## 매직 커맨드 레퍼런스 (Magic Commands)

### Lean 4 커널 매직 커맨드

| 커맨드 | 설명 |
| :--- | :--- |
| `%help`, `%lsmagic` | 사용 가능한 모든 Lean 4 매직 커맨드 도움말 출력 |
| `%env` | 현재 활성 `env`, `proofState`, Lean 버전, Lake 프로젝트 경로 확인 |
| `%env <id \| none>` | 특정 스냅샷 번호 `<id>`(또는 초기 루트 `none`)로 환경 전환/백트래킹 |
| `%history` | 세션에 기록된 모든 `env` 및 `proofState` 스냅샷 계보 출력 |
| `%infoview [auto \| on \| off]` | 직전 실행 셀의 대화형 InfoView를 강제 호출하거나 자동 표시 모드 설정 |
| `%%tactic [<id>]` | 멀티라인 전술 셀 실행 (`active_proof_state` 또는 지정한 `<id>` 대상) |
| `%tactic [<id>] <전술>` | 한 줄 전술 실행 |
| `%goals [<id>]` | 현재(또는 `<id>`) `proofState`의 열린 목표(`⊢`) 조회 및 선택 |
| `%undo` | 직전 `(env, proofState)` 상태로 한 단계 되돌리기 |
| `%reset [--hard]` | 루트 환경으로 초기화 (`--hard` 지정 시 백그라운드 `repl` 프로세스 재시작) |
| `%load [--fresh] <file.lean>` | REPL File Mode를 사용해 외부 `.lean` 파일 로드 |
| `%pickle <file.olean> [--proof]` | 현재 `env`(또는 `--proof` 지정 시 `proofState`)를 `.olean` 파일로 저장 |
| `%unpickle <file.olean> [--proof]` | 저장된 `.olean` 스냅샷 복원 *(참고: 커널 상수만 직렬화되므로 복원 세션에서는 `#reduce` 또는 `rfl` 사용)* |
| `%project [<dir>]` | 활성 Lake 프로젝트 디렉터리 조회 또는 전환 (`lake env repl`) |
| `%config [<key> [<val>]]` | 커널 옵션 조회/변경 (`all_tactics`, `root_goals`, `infotree`, `auto_backtrack`, `show_sorry_warnings`, `timeout`) |
| `%%repl_json` / `%repl_json` | `leanprover-community/repl`에 원시 JSON 요청을 직접 전송하고 응답 확인 |

### Rocq (Coq) 커널 매직 커맨드

| 커맨드 | 설명 |
| :--- | :--- |
| `%help`, `%lsmagic` | 사용 가능한 모든 Rocq 매직 커맨드 도움말 출력 |
| `%goals` | 현재 STM 상태의 활성 증명 목표(`⊢`) 및 가설 목록 출력 |
| `%state [<id>]` | 현재 STM 상태 번호 확인 또는 특정 상태 `#<id>`로 백트래킹 |
| `%history` | 현재 세션의 활성 STM 상태 계보(State Lineage) 출력 |
| `%undo` | 직전 셀 실행 트랜잭션 이전의 STM 상태로 되돌리기 |
| `%reset [--hard]` | 루트 상태로 초기화 (`--hard` 지정 시 백그라운드 `coqidetop` 프로세스 재시작) |
| `%load [--fresh] <file.v>` | 외부 Rocq/Coq (`.v`) 소스 파일을 세션에 순차 실행 |

---

## 테스트 실행

```bash
# 전체 테스트 실행
.venv/bin/pytest

# Rocq 프로토콜 및 세션 테스트
.venv/bin/pytest tests/test_rocq_protocol.py tests/test_rocq_session.py

# Lean 4 REPL 및 세션 테스트
.venv/bin/pytest tests/test_repl_protocol.py tests/test_session.py

# Web Platform 테스트
.venv/bin/pytest tests/test_web_platform.py
```
