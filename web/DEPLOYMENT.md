# 운영 배포

공개 주소: **https://proof-notebook.p-e.kr**. `/ide`와 `/ide/`는 workspace 쿼리를 보존해 `/irade/`로 308 리다이렉트합니다.

## 구성

- systemd: `/etc/systemd/system/irade-web.service` (선택적 호스트 UI 서비스), 사용자 `steam`, 내부 HTTPS `127.0.0.1:8090` (Docker 구성에서는 `irade-web` 컨테이너가 내부 TLS로 동작).
- 공개 nginx: 분리된 가상 호스트 [proof-notebook.p-e.kr.conf](../wall/deploy/nginx/proof-notebook.p-e.kr.conf)가 Let's Encrypt 인증서(`/etc/letsencrypt/live/proof-notebook.p-e.kr/`)로 TLS를 종단하고 `https://127.0.0.1:8453`(IRADE Wall Nginx)으로 `proxy_ssl_verify on` 프록시합니다.
- 외부는 Let's Encrypt 인증서(`wall/scripts/public-tls.sh`), 내부 컨테이너 간 통신은 IRADE 내부 CA(`root-ca.pem`) 검증을 사용합니다. 자세한 절차는 [PUBLIC_TLS.md](../backend/docs/PUBLIC_TLS.md)를 참고하세요.
- 운영 UI: `/home/steam/Github/IRADE/web/dist/studio/` 또는 `--studio-path`로 지정한 릴리스 디렉터리. 서버는 시작 시 파일을 메모리에 읽으며 소스·개발 파일을 공개하지 않고 HTTPS만 허용합니다.
- 외부 마켓플레이스, VS Code 다운로드와 워크벤치 경로를 운영 서버에서 제공하지 않습니다. Monaco와 worker, WASM은 번들에 포함됩니다. 와일드카드 DNS·인증서가 필요하지 않습니다.

서버 자체 NAT loopback 문제에 대한 `/etc/hosts`의 해당 도메인→`127.0.0.1` 매핑은 유지합니다. 외부 DNS는 변경하지 않았습니다.

## 갱신

1. [README](README.md)의 빌드와 테스트(`npm --prefix /home/steam/Github/IRADE/web run check && npm test && npm run test:studio`)를 실행합니다.
2. `web/dist/studio/`를 `/opt/irade-web/releases/<릴리스>/studio/`로 복사합니다. 소스와 테스트 데이터는 제외합니다.
3. [서비스 템플릿](deploy/irade-web.service)의 `WorkingDirectory=/home/steam/Github/IRADE/web` 및 인증서·사용자를 서버에 맞추고 `--studio-path`를 지정합니다. `systemctl daemon-reload`, `systemctl restart irade-web.service`를 실행합니다.
4. [nginx 템플릿](deploy/irade-web.conf)을 적용하고 `nginx -t` 성공 후 reload합니다. 프록시 CA 설정은 기존 `irade-web-proxy.conf`를 사용합니다.
5. `systemctl is-active irade-web.service agora-runtime-manager.service`, 공개 `/irade/`의 HTTP 200, `/ide/`의 308·쿼리 보존, manifest의 `/irade/` scope, 실제 브라우저 로딩과 네트워크 요청을 확인합니다.

Spring ORCID API와 DB의 `2026-11-13-irade-orcid.sql` 마이그레이션을 함께 적용합니다. Wall nginx와 공개 nginx는 ORCID 콜백의 쿼리 로깅을 끄고 `no-referrer`를 적용합니다. C++·VM 셀 동기화 프로토콜은 이전 배포를 그대로 사용합니다. 기존 VM과 파일은 보존합니다. 이전 설정과 릴리스 디렉터리를 보관해 서비스 경로와 nginx 설정을 되돌릴 수 있습니다. 인증 정보가 있을 수 있는 서버 백업은 저장소에 추가하지 않습니다.

ORCID 로그인 페이지는 `/irade/login`입니다. `/api/irade/auth/orcid/status`가 `available:true`인지 확인하고, 실제 ORCID 인증은 등록된 Redirect URI `https://proof-notebook.p-e.kr/api/irade/auth/orcid/callback`으로 돌아와야 합니다. 클라이언트 설정은 저장소 밖 `/home/steam/.config/irade/orcid.env`(0600)에서 관리하고 Spring Compose가 raw env file로 읽습니다. 비밀키를 출력하는 `docker compose config` 대신 `config --quiet`를 사용합니다.
