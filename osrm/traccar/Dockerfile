FROM traccar/traccar:6.17.0-alpine AS upstream

# The traccar-web submodule revision shipped with server v6.17.0.
FROM --platform=$BUILDPLATFORM node:24-bookworm AS web
WORKDIR /src
ADD --checksum=sha256:e716c499b7f7c67eb6a9d960d067acea1481e34c905ad325cfb18990d1804c90 https://codeload.github.com/traccar/traccar-web/tar.gz/09c926c245bb3e30d0f70f9541da4637e15496ee /tmp/web.tar.gz
RUN tar -xzf /tmp/web.tar.gz --strip-components=1 && npm ci --no-audit --no-fund
COPY replay.patch /tmp/replay.patch
RUN patch -p1 < /tmp/replay.patch
COPY web/ src/other/osrm/
RUN npm run build

# Compile the resource against the exact libraries from the release image.
FROM --platform=$BUILDPLATFORM eclipse-temurin:25-jdk AS api
WORKDIR /src
COPY --from=upstream /opt/traccar/lib/ lib/
COPY --from=upstream /opt/traccar/tracker-server.jar .
COPY OsrmResource.java .
RUN javac -proc:none -cp 'tracker-server.jar:lib/*' -d classes OsrmResource.java \
    && jar --update --file tracker-server.jar -C classes .

FROM upstream
COPY --from=api /src/tracker-server.jar /opt/traccar/
COPY --from=web /src/build/ /opt/traccar/web/
COPY --from=web /src/LICENSE.txt /opt/traccar/web/LICENSE.txt
