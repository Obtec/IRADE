# IRADE Research Workspace Web Studio

**https://irade.p-e.kr/**

대화, 증명(Lean 4 · Rocq), 코드(Python), 문서(Markdown · HTML)를 하나의 독립 Workspace에서 실시간으로 편집하고 실행하는 웹 연구 스튜디오입니다.

## 아키텍처

```text
IRADE Web Studio (Monaco + Automerge CRDT + Item ACL)
  → ORCID 인증 · Workspace Access API
  → HTTPS Gateway & Realtime Broker
  → C++ Sync Server & Workspace Store (../backend/cpp + ../db)
  → Runtime Manager → 격리 실행 VM
```

- Web Studio의 로그인, 셀 편집, 실시간 동기화는 문서화된 Workspace API와 WSS 인터페이스를 통해 연결합니다. 웹 UI는 독립 이미지로 빌드하고 배포합니다.
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
