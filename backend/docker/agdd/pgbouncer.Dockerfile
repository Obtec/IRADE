FROM alpine:3.24
RUN apk add --no-cache pgbouncer \
    && addgroup -S -g 10070 pgbouncer && adduser -S -D -H -u 10070 -G pgbouncer pgbouncer
COPY pgbouncer-entrypoint.sh /entrypoint.sh
USER pgbouncer
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
