FROM node:22-bookworm-slim AS build

WORKDIR /app
COPY package.json tsconfig.json build.mjs ./
RUN npm install --no-audit --no-fund
COPY src/ src/
COPY studio/ studio/
RUN npm run check && npm run build && npm prune --omit=dev

FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 coq \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 irade \
    && useradd --system --uid 10001 --gid irade --home-dir /home/irade --shell /usr/sbin/nologin irade

WORKDIR /app
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY assets/ ./assets/
COPY scripts/ ./scripts/

USER 10001:10001
EXPOSE 8090
CMD ["node", "scripts/serve-https.mjs", "--cert", "/run/tls/fullchain.pem", "--key", "/run/tls/privkey.pem", "--host", "0.0.0.0", "--port", "8090", "--base-path", "/", "--studio-path", "/app/dist/studio"]
