# 현재 서버 배포

공개 주소: https://frelog.p-e.kr/ide/

## 실행 구성

- systemd: `/etc/systemd/system/irade-web.service`, 사용자 `steam`, 내부 주소 `https://127.0.0.1:8090`.
- nginx: `/etc/nginx/sites-available/frelog.conf`에서 `/etc/nginx/snippets/irade-web.conf`를 포함합니다. `/ide/`는 접두사를 제거해 전달하고 `/static/`, `/irade-assets/`, `/callback`도 웹 서버로 전달합니다. 기존 Agora API와 Wall 경로는 유지합니다.
- 외부 인증서는 기존 frelog Let's Encrypt 인증서, 내부 연결은 기존 Agora 인증서와 CA 검증을 사용합니다.
- 확장은 `/opt/irade-web/releases/20261113-workspace/`의 최소 배포 파일을 사용합니다. 소스 저장소와 테스트 산출물 전체를 공개하지 않습니다.
- VS Code 빌드는 `--build-path`로 고정합니다. 서비스의 실제 경로는 `systemctl cat irade-web.service`에서 확인합니다.

실행 옵션은 `--base-path /ide/ --worker-origin https://frelog.p-e.kr --extension-path <확장 배포 경로> --build-path <VS Code 빌드 경로>`입니다. 이 구성은 확장 worker를 같은 HTTPS 출처에서 실행하므로 worker용 와일드카드 DNS/인증서가 필요하지 않습니다. 임의의 웹뷰에 대한 와일드카드 호스팅까지 구성하지는 않습니다.

서버 자체의 NAT loopback 문제 때문에 `/etc/hosts`에서 이 도메인만 `127.0.0.1`로 연결합니다. 외부 DNS는 변경하지 않았습니다.

## 갱신과 확인

1. README의 순서로 BE, Wall, 하이퍼바이저와 확장을 빌드합니다. 실행 중인 기존 VM의 에이전트 갱신은 유지보수 절차를 따릅니다.
2. 새로운 확장 배포 디렉터리에 `package.json`, `package.nls.json`, `dist/`, 아이콘과 라이선스 문서를 복사합니다. 개발 파일과 테스트 데이터는 제외합니다.
3. 서비스의 `--extension-path`를 새 릴리스로 변경하고 `systemctl daemon-reload`, `systemctl restart irade-web.service`를 실행합니다. 호스트 스크립트 변경도 서비스 재시작이 필요합니다.
4. `nginx -t`, `systemctl is-active irade-web.service agora-runtime-manager.service`, `curl -I https://frelog.p-e.kr/ide/`로 확인합니다. nginx 설정을 변경한 경우 검증 후 reload합니다.

최초 배포의 설정 백업과 검증 기록은 서버의 `/var/lib/irade/deployments/20261112T145658Z/`에 보관합니다. 자격 증명이 포함될 수 있는 백업은 공개하거나 저장소에 추가하지 않습니다.

실제 두 계정·두 브라우저로 동시 편집 병합, 한글 동기화, VM 재시작 후 내용 유지, 실시간 권한 회수, 파일 이름 변경과 삭제를 검증했습니다. 검증용 캔버스와 계정은 정상 API로 삭제했습니다.
