# IRADE: Interactive Reasoning & Analysis Development Environment

**IRADE**는 **Lean 4**와 **Rocq (Coq)** 등 형식 논리 및 대화형 정리 증명(Interactive Theorem Proving, ITP)을 위한 차세대 대화형 증명·노트북 개발 환경 모노레포(Monorepo)입니다.

Visual Studio Code(Code - OSS) 기반의 프론트엔드 IDE와 고성능 상주형 정리 증명 커널(`notebook-kernel`)을 통합하여, 대화형 전술 탐색(Interactive Tactic Exploration), 불변 상태 스냅샷 및 자동 롤백, 실시간 분할 InfoView를 완벽하게 지원합니다.

---

## 🏛️ 모노레포 구조 (Monorepo Architecture)

IRADE는 사용자 인터페이스와 증명 엔진 백엔드가 분리된 모노레포로 구성되어 있습니다.

```text
IRADE/
├── IDE/                         # Code - OSS (Visual Studio Code) 프론트엔드 환경
│   ├── src/                     # 핵심 에디터 및 워크벤치 소스 코드
│   ├── extensions/              # 기본 및 맞춤형 확장 기능
│   ├── build/                   # 데스크톱/웹 빌드 및 패키징 스크립트
│   └── package.json             # IDE 의존성 및 워크스페이스 설정
│
├── notebook-kernel/             # Lean 4 & Rocq 대화형 증명 노트북 커널
│   ├── src/lean4_kernel/        # Lean 4 Jupyter 커널 & Web Proof Studio
│   │   ├── repl.py              # REPL 상주 프로세스 및 JSON stdio 프로토콜
│   │   ├── session.py           # 불변 환경(env) 스냅샷 그래프 및 자동 백트래킹
│   │   ├── infoview.py          # InfoTree 파싱 및 에디터 커서 연동 InfoView
│   │   └── web.py               # Proof Studio 웹 서버 & Agora 브리지
│   ├── src/rocq_kernel/         # Rocq (Coq) Jupyter 커널
│   │   ├── driver.py            # coqidetop XML stdio 프로세스 관리자
│   │   ├── xml_protocol.py      # Coq/Rocq XML 호출 및 응답 파서
│   │   ├── session.py           # STM 상태 트리 및 문장 단위 트랜잭션 격리
│   │   └── infoview.py          # Rocq InfoView HTML split-pane 렌더러
│   ├── vendor/repl/             # Lean 4 REPL 바이너리 서브시스템
│   ├── examples/                # 증명 예제 (.lean, .v)
│   ├── notebooks/               # 데모 인터랙티브 노트북 (.ipynb)
│   └── pyproject.toml           # Python 패키지 사양 및 종속성
│
├── agora-workspace/             # Agora VM /workspace의 VS Code 웹 공동 편집 확장
└── .gitignore                   # 모노레포 공통 Git 무시 규칙
```

---

## ✨ 핵심 기능 (Key Features)

### 1. 다중 정리 증명기(Multi-Prover) 지원
- **Lean 4 통합**: [`leanprover-community/repl`](https://github.com/leanprover-community/repl) 기반의 상주형 REPL 세션을 통해 오버헤드 없는 즉각적인 셀 실행과 Mathlib 임포트를 지원합니다.
- **Rocq (Coq) 통합**: `coqidetop` XML stdio 프로토콜과 STM(State Transaction Manager)을 연동하여 Rocq Prover 9.x 및 Coq 8.14+ 버전을 완벽하게 지원합니다.

### 2. 스마트 불변 상태 스냅샷 & 자동 백트래킹
- **Lean 4 Snapshot Graph**: 셀 실행 오류 발생 시 이전 정상 환경(`env`) 스냅샷으로 자동 복구하며, 이미 정의된 식별자를 재실행하면 이전 분기점에서 새 브랜치를 생성하여 환경 오염을 방지합니다.
- **Rocq Sentence-Level Rollback**: 코드를 문장 단위(`Sentence`)로 파싱하여 실행하며, 실패 시 셀 진입 직전의 STM 상태로 안전하게 자동 롤백(`Edit_at`)합니다.

### 3. 실시간 Split-Pane InfoView
- 증명 단계마다 열려 있는 서브골(`⊢ ...`)과 지역 가설(`hypotheses`)을 시각화합니다.
- 에디터 내 커서 위치 이동에 맞춰 해당 위치의 전술 상태(`Goals Before`, `Goals After`, `Term at Cursor`)를 실시간으로 동기화합니다.

### 4. 대화형 전술 모드 (`%%tactic`) 및 백슬래시 유니코드 자동완성
- 열린 증명 목표에 대해 셀 단위로 전술을 적용해 나갈 수 있는 매직 커맨드(`%tactic`, `%%tactic`)를 제공합니다.
- 수학 및 논리 기호(`\forall` → `∀`, `\to` → `→`, `\exists` → `∃` 등) 자동완성과 `Shift+Tab` 타입/식별자 조회를 지원합니다.

---

## 🚀 빠른 시작 (Getting Started)

### 필수 요구사항 (Prerequisites)
- **Node.js** (v20 이상) 및 **pnpm** (IDE 빌드용)
- **Python** 3.10 이상 (노트북 커널 구동용)
- **Lean 4**: [`elan`](https://github.com/leanprover/elan) (`lean`, `lake`)
- **Rocq (Coq)**: `coqidetop` (`brew install coq` 또는 `opam install coq`)

---

### 1. 증명 노트북 커널 (`notebook-kernel`) 설정

```bash
cd notebook-kernel

# 가상환경 생성 및 의존성 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Lean 4 커널 설치 및 REPL 빌드
lean4-kernel install --user --build-repl
lean4-kernel doctor

# Rocq (Coq) 커널 등록 및 점검
rocq-kernel install --user
rocq-kernel doctor
```

설치 완료 후 다음 중 하나의 방법으로 증명 환경을 실행할 수 있습니다:
- **Web Proof Studio 실행**:
  ```bash
  lean4-kernel serve --host 127.0.0.1 --port 8890
  ```
- **JupyterLab 실행**:
  ```bash
  jupyter lab
  ```

---

### 2. IDE 환경 (`IDE`) 빌드 및 실행

```bash
cd IDE

# 패키지 의존성 설치
pnpm install

# 빌드 및 감시 모드 실행
pnpm run watch

# 개발 빌드 실행 (새 터미널)
./scripts/code.sh
```

---

## 🧪 테스트 실행 (Running Tests)

### Agora 작업 공간 공동 편집

[`agora-workspace`](agora-workspace/README.md)는 기존 Agora 캔버스의 VM `/workspace`를 VS Code 웹 파일 탐색기에 연결합니다. 파일·폴더 관리, Automerge 동시 편집 병합, Wall 실시간 공유와 재접속 처리를 포함합니다. 백엔드·Wall·VM 워커 적용, HTTPS 웹 IDE 실행과 검증 방법은 [설정 안내](agora-workspace/README.md)를 참고하세요.

### 커널 단위 및 E2E 테스트
```bash
cd notebook-kernel
source .venv/bin/activate
pytest
```

---

## 📜 라이선스 (License)

- `IDE/`: [MIT License](IDE/LICENSE.txt) (Microsoft Code - OSS 기반)
- `notebook-kernel/`: [Apache 2.0 / MIT Dual License](notebook-kernel/LICENSE)
