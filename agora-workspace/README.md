# Agora Shared Workspace

웹 아이콘은 `assets/irade-icon-source.png`에서 제작한 IRADE 심볼을 사용합니다. HTTP/HTTPS 실행기, IDE 소스 빌드의 브라우저 탭·홈 화면 아이콘과 확장 카드에 적용됩니다. [제작 프롬프트](assets/ICON-PROMPT.md)를 보관했으며, Pillow가 설치된 Python에서 `python3 scripts/prepare-icons.py`로 크기별 PNG와 ICO를 다시 생성할 수 있습니다.

Agora 캔버스 하나를 해당 VM의 `/workspace` 디렉터리 하나에 연결하는 VS Code 웹 확장입니다. VS Code의 파일 탐색기와 텍스트 에디터를 그대로 사용하며, 브라우저마다 독립적인 Automerge 복제본으로 편집합니다.

```text
VS Code Web + agora://<canvasId>@<Agora host>/
  → Spring /access (기존 로그인 JWT와 캔버스 비밀번호 확인)
  → Wall의 인증된 Canvas WSS
  → C++ workspace_files (접속 identity, 최신 권한 확인)
  → 기존 VM 관리자 Unix socket
  → 해당 VM의 VSOCK 5003 → /workspace (UID 1000)
```

텍스트 변경은 약 100ms 후 전송됩니다. VM은 Automerge 문서를 병합하고 실제 파일과 변경 이력을 저장한 뒤 성공을 반환합니다. Wall은 같은 캔버스의 연결에 변경 알림을 전달하고 다른 편집기는 최신 문서를 읽어 자신의 미전송 변경과 병합합니다. 동일한 요청 ID의 재전송은 VM에 저장된 처리 기록으로 중복 적용을 방지합니다. VM 디스크의 SQLite WAL과 작업 기록은 저장 도중 중단된 작업을 다음 요청에서 복구합니다.

## 빌드와 서버 적용

세 저장소의 변경을 함께 적용해야 합니다. 실행 중인 서비스에는 소스 수정만으로 반영되지 않습니다.

1. 이 디렉터리에서 확장을 빌드합니다.

   ```bash
   corepack pnpm@10.32.1 install --frozen-lockfile
   corepack pnpm@10.32.1 run check
   corepack pnpm@10.32.1 run build
   ```

2. `hypervisor-for-agora`에서 게스트의 Automerge 번들을 빌드합니다.

   ```bash
   cd ../../hypervisor-for-agora/runtime/workspace
   corepack pnpm@10.32.1 install --frozen-lockfile
   corepack pnpm@10.32.1 run build
   ```

   위 경로는 `IRADE/agora-workspace`에서 출발합니다. 생성물은 `runtime/guest/workspace-crdt.cjs`입니다. 기존 런타임 설치 절차의 `runtime/install`로 관리자와 게스트 소스를 설치하세요. 멈춰 있는 VM은 다음 일반 부팅에서 `agent_update.py`를 통해 갱신됩니다. 오래된 에이전트로 실행 중인 VM에는 `WORKSPACE_AGENT_UPGRADE_REQUIRED`를 반환하며, 이 파일 기능이 VM을 강제로 재시작하지 않습니다. 기존 `runtime/update-agent`는 모든 실행 VM이 멈춘 유지보수 시점에만 사용합니다.

3. `project-agora-BE`의 C++, Spring과 `project-agora-Wall`의 브로커를 기존 배포 절차로 갱신합니다.

   ```bash
   # project-agora-BE
   ./scripts/backend-docker.sh up
   # project-agora-Wall
   ./scripts/broker-docker.sh up
   ```

   DB 스키마 변경과 새 공개 VM 포트는 필요하지 않습니다. VM 관리자 Unix socket과 기존 Wall WSS 라우팅을 사용합니다. 새 SQLite 파일은 VM 내부 `/var/lib/agora-workspace`에 있으며 사용자 프로젝트의 파일 목록에 나타나지 않습니다.

## 웹 IDE 실행

실제 Agora 연결에는 HTTPS로 실행하세요. 아래 인증서는 `ide.example.com`과 확장 호스트의 `*.ide.example.com`에 유효한 인증서여야 합니다. DNS도 두 호스트 형태를 같은 IDE 서버로 연결해야 합니다.

```bash
cd IRADE/agora-workspace
corepack pnpm@10.32.1 run serve:https \
  --cert /path/to/ide-fullchain.pem --key /path/to/ide-key.pem \
  --host 0.0.0.0 --port 8080 \
  --folder-uri 'agora://42@agora.example.com:8443/'
```

`https://ide.example.com:8080`으로 접속합니다. 실행기는 기본적으로 공식 VS Code 웹 빌드를 내려받아 사용합니다. 이 저장소의 IDE를 빌드했다면 `--sourcesPath ../IDE`로 해당 빌드를 사용할 수 있습니다. 이 실행기는 `@vscode/test-web`을 사용한 개발·검증용 호스트입니다. 운영 배포는 빌드한 Code OSS 웹 워크벤치에 이 확장을 포함시키고 동일한 HTTPS/Origin 설정을 적용하세요.

VS Code는 확장 호스트를 `https://v--<hash>.ide.example.com:8080`에서 실행합니다. Agora의 기존 허용 출처 목록을 보존하면서 아래 출처를 추가하세요.

```dotenv
# BE .env: 기존 CORS_ALLOWED_ORIGINS 값도 보존
CORS_ALLOWED_ORIGIN_PATTERNS=https://*.ide.example.com:8080

# Wall .env: 기존 BROKER_ALLOWED_ORIGINS 값에 추가
# https://*.ide.example.com:8080
```

각 브라우저에서 기존 Agora 계정으로 로그인해 얻은 **일반 access token**을 입력합니다. 토큰은 VS Code SecretStorage에 저장하며 작업 공간 URI에 포함하지 않습니다. 캔버스 토큰은 기존 `/access`에서 따로 발급받습니다. 비밀번호가 있는 캔버스는 최초 연결 때 비밀번호도 묻습니다. 로그인 토큰 만료 후에는 **Agora: Set Access Token**으로 갱신할 수 있습니다.

명령 팔레트의 **Agora: Open Shared Workspace**에서 Agora HTTPS 주소와 캔버스 ID를 입력해도 됩니다. **Agora: Copy Workspace Link**는 `agora://42@agora.example.com:8443/` 같은 URI를 복사합니다. 다른 사용자는 같은 URI로 작업 공간을 열고 자신의 계정으로 인증합니다. 기존 Agora의 캔버스 초대·공개 여부·그룹 설정이 접속을 결정합니다.

TLS 없이 UI만 살펴보려면 `pnpm run serve --port 8080`을 사용할 수 있습니다. 기존 Agora 브로커는 HTTPS 출처를 요구하므로 실제 서비스 연결에는 위 HTTPS 실행 방법을 사용하세요.

## 동작과 제한

- 작업 공간의 권한 단위는 **캔버스 전체 디렉터리**입니다. 캔버스 읽기 권한은 `/workspace` 전체의 읽기 권한, 쓰기 권한은 파일·폴더 변경 권한에 대응합니다. 캔버스 객체별 ACL은 파일별 ACL로 변환하지 않습니다.
- 텍스트는 편집하면서 자동으로 공유·저장됩니다. Save는 전송 완료를 기다립니다. 편집기의 미저장 표시와 공유 저장 상태는 다르며, Discard가 이미 공유된 변경을 취소하지 않습니다. 연결이 끊기면 현재 탭의 복제본과 요청을 유지해 재접속 때 병합합니다. 탭 종료를 견디는 별도의 오프라인 CRDT 저장소는 제공하지 않습니다.
- 현재 파일 내용과 Automerge 저장 문서는 각각 128 KiB 이하입니다. 이력 크기 한도를 넘으면 저장을 거부하고 로컬 편집 내용은 유지합니다. 디렉터리 목록은 256개씩 조회하며 디렉터리당 10,000개 항목까지 지원합니다.
- 바이너리는 실시간 문자 병합 대신 SHA-256 etag로 저장 충돌을 거부합니다. 이름 변경의 대상이 이미 있으면 기존 파일을 보존하고 충돌을 반환합니다. 심볼릭 링크와 특수 파일은 지원하지 않습니다.
- 같은 파일에 대한 협업 편집은 CRDT로 병합됩니다. 터미널에서 일반 쓰기로 변경한 파일은 열린 파일의 5초 조회에 반영합니다. 외부 프로그램이 파일을 교체한 경우에는 새 파일 세대로 취급하고 오래된 편집기의 저장을 거부합니다. 임의의 외부 프로세스와 원자적 교체 직전까지 상호 잠금하는 기능은 없습니다.
- 노트북·바이너리 및 가상 작업 공간을 지원하는 확장 기능은 VS Code의 저장 시점 동작을 따릅니다. VM의 LSP·디버거·터미널을 VS Code에 새로 원격 연결하는 기능은 이 확장의 범위에 포함되지 않습니다.

## 검증

```bash
# 이 디렉터리
corepack pnpm@10.32.1 run check
corepack pnpm@10.32.1 run test
corepack pnpm@10.32.1 run build
corepack pnpm@10.32.1 run test:web

# hypervisor-for-agora
python3 -m unittest discover -s runtime/tests -p 'test_*.py'
```

웹 테스트는 Chrome과 OpenSSL을 사용하며, `AGORA_HYPERVISOR_DIR`로 하이퍼바이저 소스 위치를 지정할 수 있습니다. 실제 VS Code 웹 에디터와 실제 게스트 파일/Automerge 구현 사이에 격리된 HTTPS/WSS 테스트 게이트웨이를 둡니다. 운영 Agora 계정, 실행 VM, DB는 사용하지 않습니다. Wall은 `broker/test/room_test.exs`, BE는 C++ `workspace_files_tests.cpp`와 Spring `WorkspaceCorsTest`에서 별도로 검증합니다.
