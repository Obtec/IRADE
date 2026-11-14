# IRADE Research Workspace Web Studio

**https://frelog.p-e.kr/irade/**

대화, 증명(Lean 4 · Rocq), 코드(Python), 문서(Markdown · HTML)를 하나의 독립 Workspace에서 실시간으로 편집하고 실행하는 웹 연구 스튜디오입니다.

## 아키텍처

```text
IRADE Web Studio (Monaco + Automerge CRDT + Item ACL)
  → ORCID 인증 · Workspace Access (../backend/spring)
  → Wall Nginx & Elixir Broker (../wall)
  → C++ Sync Server & Workspace Store (../backend/cpp + ../db)
  → Runtime Manager (/run/agora-runtime) → 격리 실행 VM
```

- 모든 Workspace와 셀(`Workspace Item ID`)은 독립 DB(`workspace_info`), 독립 Elasticsearch(`workspace`), 독립 Redis(`workspace:{id}`), 독립 JWT(`workspace-access` / `workspaceId`)를 사용합니다.
- 레거시 VS Code Web 확장(`extension.ts`, `@vscode/test-web`) 및 VM 파일트리(`workspace_files`) 프로토콜은 제거되었습니다.

## 빌드 및 검증

```bash
cd /home/steam/Github/IRADE/web
npm run check
npm test
npm run build
npm run test:studio
```

- `npm run check`: TypeScript 타입 검사 (`tsc --noEmit`)
- `npm test`: 클라이언트 연결·문서 유틸·Workspace CRDT 및 Item ACL 단위 테스트 (`test/run.mjs`)
- `npm run build`: Monaco/Automerge 브라우저 번들 생성 (`dist/studio/`)
- `npm run test:studio`: Playwright Headless Chrome E2E 테스트 (ORCID 로그인, MFA 재시도, Workspace 생성/선택, 셀 CRUD, 동시 CRDT 병합, 새로고침 복원, 권한 변경, 미리보기 격리, 모바일 레이아웃)

운영 배포 안내는 [DEPLOYMENT.md](DEPLOYMENT.md)를 참고하세요.
