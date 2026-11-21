# IRADE 공개 TLS

공개 주소는 `https://irade.p-e.kr`입니다. 인증서는 호스트 Nginx에서 종단하고, Nginx는 내부 CA와 이름 검증을 통해 IRADE 서비스 게이트웨이로 프록시합니다.

## 인증서 발급

도메인의 A 레코드가 외부에서 접근 가능한 호스트를 가리켜야 합니다. 현재 환경은 TCP/80·443이 외부에서 도달하지 않아 DNS-01 검증을 사용합니다.

```bash
certbot certonly --manual --preferred-challenges dns --key-type ecdsa \
  --cert-name irade.p-e.kr -d irade.p-e.kr --agree-tos \
  --register-unsafely-without-email --no-eff-email \
  --config-dir "$HOME/.config/irade/certbot" \
  --work-dir "$HOME/.local/state/irade/certbot-work" \
  --logs-dir "$HOME/.local/state/irade/certbot-log"
```

Certbot이 출력하는 TXT 값을 DNS에 추가합니다.

```text
_acme-challenge.irade.p-e.kr  TXT  <Certbot이 출력한 값>
```

DNS 전파를 확인하고 Certbot 프롬프트를 이어갑니다. TXT 값은 검증 후 제거할 수 있습니다. 발급 인증서를 Nginx용 사용자 경로에 복사합니다.

```bash
./wall/scripts/public-tls.sh sync irade.p-e.kr \
  --source-dir "$HOME/.config/irade/certbot/live/irade.p-e.kr" --no-reload
```

수동 DNS 방식은 자동 갱신되지 않습니다. 만료 전에 [갱신 스크립트](../../web/deploy/renew-irade-certificate.sh)를 실행해 TXT 검증을 다시 완료하세요. DNS 제공자의 API가 준비되면 DNS 플러그인으로 자동 갱신을 구성할 수 있습니다.

## Nginx 적용

[도메인 가상 호스트](../../web/deploy/irade.p-e.kr.conf)는 호스트 Nginx에 설치합니다. 외부 HTTPS 진입은 기존 proxy-protocol 리스너 `127.0.0.1:9443`으로 전달되어야 합니다. 이 설정은 사용자 인증서 경로, 내부 CA 검증, 실행 API 시간 제한, WebSocket 업그레이드, HTTP→HTTPS 리다이렉트를 포함합니다.

```bash
sudo install -m 0644 web/deploy/irade.p-e.kr.conf /etc/nginx/sites-available/irade.p-e.kr.conf
sudo ln -sfn /etc/nginx/sites-available/irade.p-e.kr.conf /etc/nginx/sites-enabled/irade.p-e.kr.conf
sudo nginx -t
sudo systemctl reload nginx
```

호스트 포트 80을 열 수 있으면 `/.well-known/acme-challenge/` 경로에서 HTTP-01 파일을 제공합니다. 현재 공개 경로가 닫혀 있을 때는 DNS-01을 사용합니다. 공개 Let's Encrypt 인증서는 내부 서비스 CA나 데이터베이스 인증서와 분리해 보관합니다.

## 서비스 라우팅

호스트 Nginx는 웹 요청을 내부 HTTPS 게이트웨이로 전달합니다. Wall Nginx는 `/api/`를 Spring으로, `/wss/`를 실시간 브로커로, 웹 페이지·정적 파일과 `/api/execute`, `/api/metrics`를 IRADE Web으로 전달합니다. 내부 라우팅은 [irade.conf.example](../../wall/nginx/irade.conf.example)에 있습니다.

새 도메인에 적용한 뒤 `/`, `/assets/manifest.webmanifest`, `/api/irade/auth/orcid/status`, WebSocket 연결을 확인합니다. ORCID 앱에는 application URL `https://irade.p-e.kr/`와 Redirect URI `https://irade.p-e.kr/api/irade/auth/orcid/callback`을 등록해야 합니다.
