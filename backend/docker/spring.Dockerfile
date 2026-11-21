FROM amazoncorretto:27.0.1-al2023 AS build

WORKDIR /workspace

COPY spring/gradlew spring/gradlew
COPY spring/gradle/ spring/gradle/
COPY spring/settings.gradle spring/build.gradle spring/gradle.lockfile spring/
COPY spring/src/ spring/src/
COPY LICENSE THIRD_PARTY_LICENSES.md ./
COPY licenses/ licenses/
COPY cpp/third_party/uWebSockets/LICENSE cpp/third_party/uWebSockets/LICENSE

RUN chmod +x spring/gradlew \
    && ./spring/gradlew --no-daemon -p spring bootJar

FROM amazoncorretto:27.0.1-al2023-headless

RUN dnf install -y ca-certificates curl-minimal shadow-utils \
    && dnf clean all \
    && groupadd --system --gid 10001 irade \
    && useradd --system --uid 10001 --gid irade --home-dir /home/irade --shell /usr/sbin/nologin irade \
    && mkdir -p /home/irade/irade/workspace-resource \
    && chown -R 10001:10001 /home/irade

WORKDIR /app
COPY --from=build --chown=10001:10001 /workspace/spring/build/libs/frelog-0.0.1-SNAPSHOT.jar /app/app.jar

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
