# IRADE Research Workspace

**https://frelog.p-e.kr/irade/**

대화, 증명, 코드, 문서를 하나의 workspace에서 편집하고 공유하는 웹 연구 환경입니다. 기존 `/ide/` 링크는 쿼리 매개변수를 유지하며 `/irade/`로 이동합니다.

Antigravity의 에이전트 중심 배치, VS Code의 편집기·명령 탐색, IntelliJ의 도구 패널 구성을 참고했습니다. 왼쪽은 workspace와 셀 탐색, 가운데는 셀 편집, 오른쪽은 컨텍스트·활동 패널입니다. IRADE 이미지와 아이콘, 짙은 회색 바탕과 민트 강조색을 사용합니다. VS Code 시작 화면, 브랜드 이미지, 확장 마켓플레이스를 운영 화면에서 제거했습니다. 코드 편집에는 Monaco를 사용하며 외부 마켓플레이스나 VS Code CDN에 요청하지 않습니다.

## 사용 방법

**Workspace 열기 → IRADE ORCID 로그인 페이지 → ORCID 인증 → workspace 선택**으로 시작합니다. 전용 로그인 주소는 **https://frelog.p-e.kr/irade/login** 입니다. 최초 인증 시 연구자 계정이 Agora DB에 등록되며 이메일 공개나 별도 IRADE 비밀번호가 필요하지 않습니다. 기존 Agora workspace·권한을 유지하려면 로그인 페이지의 **기존 Agora 계정에 ORCID 연결하기**에서 해당 계정을 확인한 뒤 연결합니다. 계정에 MFA가 설정되어 있으면 추가 인증을 요청합니다.

Workspace 목록과 검색은 Spring Boot의 `/api/irade/workspaces?q=...`에서 공개 범위·참여 권한을 확인합니다. 새 workspace는 기본 비공개로 생성되며 공유 링크·ID로 접근할 수도 있습니다. 초대·권한 관리는 기존 Agora 설정을 따릅니다. 인증된 ORCID 프로필은 상단 **ORCID · 계정**에서 확인할 수 있습니다.

화면의 작업 단위는 파일·폴더가 아닌 **셀**입니다.

| 셀 | 기능 |
| --- | --- |
| 에이전트 대화 기록 | 사용자·에이전트·연구 메모 메시지 기록과 Markdown 편집·미리보기 |
| Lean 4 / Rocq 증명 | 언어별 구문 강조, 증명 소스 편집·공유, 증명 상태 패널 |
| Python 코드 | Monaco 코드 편집, 자동 저장과 실시간 병합 |
| Markdown / HTML 문서 | 문서 편집과 격리된 미리보기 |

셀을 만들고 선택하거나 검색·내보내기·삭제할 수 있습니다. `Ctrl K`는 셀 검색, `Alt N`은 새 셀, `Ctrl S`는 저장, `Ctrl Shift F`는 집중 모드입니다. 편집기 글자 크기와 줄 번호를 조절할 수 있으며 작은 화면에 맞춰 패널 배치가 바뀝니다.

계정 없이 **로컬 체험**을 선택하면 예제 셀을 편집할 수 있습니다. 체험 내용은 현재 탭의 `sessionStorage`에만 저장되며 다른 사용자에게 공유되지 않습니다. ORCID 로그인으로 발급한 애플리케이션 토큰은 `irade.orcid.token:<origin>` 키로 현재 탭에만 보관합니다. ORCID 비밀번호는 수집하지 않고, OAuth 코드 교환과 클라이언트 비밀키는 Spring 서버에서 처리합니다. 브라우저 이동 URL에 로그인 토큰을 포함하지 않습니다.

현재 AI 응답 생성과 Python·Lean 4·Rocq 실행 커널은 새 셀 화면에 연결하지 않았습니다. 대화 기록과 소스 편집·동기화·문서 미리보기가 동작하며, 실행 결과나 증명 성공을 생성하지 않습니다.

## 저장과 동기화

```text
IRADE workspace + Monaco
  → ORCID 인증 → Spring / IRADE workspace access
  → Wall 인증 WSS
  → C++ workspace_files
  → VM 관리자 / VSOCK 5003
  → VM /workspace 내부 셀 저장소
```

Agora 캔버스 하나가 VM의 `/workspace` 하나에 대응합니다. 셀은 내부적으로 `/.irade/cells/<시간>-<UUID>.<종류>`에 각각 저장되며 사용자에게 파일 트리를 노출하지 않습니다. 기존 `/workspace` 파일은 이동·삭제하지 않습니다. 각 셀의 Automerge 문서를 독립적으로 병합하므로 서로 다른 셀의 동시 생성은 공유 JSON 인덱스를 덮어쓰지 않습니다. 셀 순서는 생성 시간과 UUID로 결정됩니다.

편집 변경은 약 150ms 후 전송합니다. 연결이 끊기면 현재 탭의 복제본을 유지하며 재접속 때 병합합니다. 저장하지 못한 편집이 있으면 탭 종료 시 확인을 요청합니다. 다른 참여자가 열린 셀을 삭제하면 현재 편집 내용을 내려받아 보관할 수 있습니다. VM의 SQLite WAL과 요청 처리 기록을 통한 복구·중복 방지는 기존 구현을 사용합니다.

읽기·쓰기 권한은 workspace 전체 단위이며 변경 시 편집기에 반영됩니다. 파일별 ACL로 변환하지 않습니다. 내용과 Automerge 이력은 각각 128 KiB 이하입니다. 새로고침·탭 종료를 견디는 별도 오프라인 CRDT 저장소는 없습니다. HTML 미리보기는 DOMPurify와 `sandbox` iframe, 별도 CSP로 격리하며 스크립트를 실행하지 않습니다.

기존 VS Code 가상 파일 확장(`src/extension.ts`)과 프로토콜은 호환성·회귀 테스트용으로 유지합니다. 운영 HTTP 서버는 기본 VS Code 워크벤치와 마켓플레이스를 제공하지 않습니다.

## 빌드와 실행

```bash
cd agora-workspace
corepack pnpm@10.32.1 install --frozen-lockfile
corepack pnpm@10.32.1 run check
corepack pnpm@10.32.1 run build
corepack pnpm@10.32.1 run serve:https \
  --cert /path/to/fullchain.pem --key /path/to/privkey.pem \
  --host 127.0.0.1 --port 8090 --base-path /irade/
```

같은 HTTPS 출처에서 기존 Agora `/api/`, `/wss/`와 웹 UI를 프록시합니다. 실행 커널·동기화 백엔드 배포는 기존 Agora 절차를 따릅니다. 게스트 Automerge 번들은 `hypervisor-for-agora/runtime/workspace`에서 빌드한 뒤 `bash runtime/install`로 설치합니다. 실행 중인 VM을 강제로 재시작하지 않습니다.

운영 서버 구성과 릴리스 경로는 [DEPLOYMENT.md](DEPLOYMENT.md), 서버 템플릿은 [deploy/](deploy/)를 참고하세요. HTTP 로컬 미리보기에는 `pnpm run serve`를 사용할 수 있습니다. 실제 Agora 연결은 HTTPS가 필요합니다.

## 검증

```bash
corepack pnpm@10.32.1 run check
corepack pnpm@10.32.1 run test
corepack pnpm@10.32.1 run build
corepack pnpm@10.32.1 run test:studio
corepack pnpm@10.32.1 run test:web
```

Studio 테스트는 두 Chrome 세션과 격리된 실제 게스트 파일/Automerge 구현으로 로그인·workspace 선택, 동시 편집 병합, 셀 동시 생성, 새로고침 복원, 권한 변경, 셀 삭제, 문서 미리보기 격리와 모바일 배치를 검증합니다. ORCID 제공자와 인증 게이트웨이는 테스트 fixture이며 운영 계정·DB·VM은 사용하지 않습니다. 기존 `test:web`은 VS Code 가상 파일 확장의 회귀 테스트입니다. 스크린샷은 버전 관리에서 제외한 `.artifacts/`에 생성됩니다.

프로젝트 아이콘 원본은 `assets/irade-icon-source.png`입니다. [제작 프롬프트](assets/ICON-PROMPT.md)를 보관하며 `python3 scripts/prepare-icons.py`로 파생 아이콘을 생성할 수 있습니다.

ORCID DB 마이그레이션·인증 API·운영 자격 증명 설정은 [Spring ORCID 안내](../../project-agora-BE/docs/IRADE_ORCID.md)를 참고하세요. 서버의 보호된 ORCID 환경 파일은 `/home/steam/.config/irade/orcid.env`이며 저장소에 포함하지 않습니다.
