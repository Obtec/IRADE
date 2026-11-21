# 운영 배포

IRADE Web Studio의 공개 주소는 **https://irade.p-e.kr/**입니다. Nginx가 웹 앱, Spring API, 실행 API, WebSocket 브로커를 URL 경로에 따라 내부 서비스로 전달합니다.

| 경로 | 내부 서비스 |
| --- | --- |
| `/`, `/studio`, `/login`, `/assets/`, `/preview` | IRADE Web (`irade-web:8090`) |
| `/api/execute`, `/api/metrics` | IRADE Web 실행·상태 API |
| `/api/`, `/api/irade/` | Spring API (`irade-spring:8080`) |
| `/wss/` | 실시간 브로커 (`irade-broker:4443`) |

443 포트의 TLS SNI 라우터가 도메인별 loopback HTTPS listener로 연결합니다. IRADE는 별도 `9446` listener를 사용하며, [독립 도메인 가상 호스트](deploy/irade.p-e.kr.conf)가 공개 인증서를 제시하고 내부 게이트웨이의 CA도 검증합니다. Wall Nginx의 서비스별 라우팅은 [irade.conf.example](../wall/nginx/irade.conf.example)에 있습니다.

## 인증서와 Nginx

새 Let's Encrypt 인증서는 DNS-01 방식으로 발급합니다. Certbot은 계정과 인증서를 사용자 전용 디렉터리에 보관합니다. 아래 명령을 실행하고 Certbot이 표시하는 `_acme-challenge.irade.p-e.kr` TXT 값을 DNS에 등록합니다.

```bash
certbot certonly --manual --preferred-challenges dns --key-type ecdsa \
  --cert-name irade.p-e.kr -d irade.p-e.kr --agree-tos \
  --register-unsafely-without-email --no-eff-email \
  --config-dir "$HOME/.config/irade/certbot" \
  --work-dir "$HOME/.local/state/irade/certbot-work" \
  --logs-dir "$HOME/.local/state/irade/certbot-log"
./wall/scripts/public-tls.sh sync irade.p-e.kr \
  --source-dir "$HOME/.config/irade/certbot/live/irade.p-e.kr" --no-reload
```

발급이 끝나면 인증서는 `/home/steam/.local/share/irade/letsencrypt/irade.p-e.kr/`에 설치됩니다. 저장소 루트에서 설치 스크립트를 실행하면 기존 443 SNI map에 IRADE 도메인만 9446으로 보내는 항목을 추가하고, 가상 호스트를 활성화한 뒤 Nginx를 검증·재적용합니다.

```bash
sudo web/deploy/activate-irade-vhost.sh
```

수동 DNS 방식은 자동 갱신되지 않으므로 만료 전에 [갱신 스크립트](deploy/renew-irade-certificate.sh)를 실행해 TXT 검증을 다시 완료해야 합니다. DNS 제공자의 자동화 API가 준비되면 DNS 플러그인으로 갱신을 자동화할 수 있습니다. 포트 80을 외부에서 열 수 있는 환경에서는 Nginx의 ACME HTTP-01 경로도 사용할 수 있습니다.

Wall 서비스 게이트웨이에 내부 라우팅 변경을 적용하려면 `wall/.env`의 `NGINX_PUBLIC_HOSTNAME=irade.p-e.kr`를 설정하고 다음을 실행합니다.

```bash
./wall/scripts/nginx-docker.sh production
```

## 웹 앱 배포

저장소의 `web/` 디렉터리에서 이미지를 빌드하고 서비스를 갱신합니다. 컨테이너 기본 경로는 도메인 루트(`/`)입니다.

```bash
docker compose up -d --build web
docker compose ps
```

배포 확인:

```bash
curl --fail --silent --show-error https://irade.p-e.kr/ --output /dev/null
curl --fail --silent --show-error https://irade.p-e.kr/assets/manifest.webmanifest
curl --fail --silent --show-error https://irade.p-e.kr/api/irade/auth/orcid/status
```

## ORCID 로그인

ORCID Public API 클라이언트의 application URL은 `https://irade.p-e.kr/`, Redirect URI는 `https://irade.p-e.kr/api/irade/auth/orcid/callback`으로 등록합니다. 인증 환경 파일은 저장소 밖 `/home/steam/.config/irade/orcid.env`에서 관리합니다. 설정과 재시작 절차는 [IRADE ORCID 안내](../backend/docs/IRADE_ORCID.md)를 참고하세요.

기존 `/irade/...` 및 `/ide/...` 공유 링크는 새 도메인으로 옮길 수 있도록 [FreLog 이전 경로 redirect 설정](deploy/frelog-irade-redirect.conf)을 제공합니다. 새 서비스 주소와 API는 `irade.p-e.kr`만 사용합니다.
