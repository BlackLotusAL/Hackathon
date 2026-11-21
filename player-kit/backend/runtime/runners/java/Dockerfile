FROM maven:3.9.9-eclipse-temurin-17-noble@sha256:578027eef9bd2191bad0fa011e0a352b1b92b883e5ec4ac30d1a96d9c3f3e9b5 AS build
ARG ARENA_MAVEN_MIRROR_URL=https://repo.maven.apache.org/maven2
WORKDIR /src
COPY pom.xml .
COPY src ./src
COPY --from=runner /Main.java /StateDecoder.java ./src/main/java/arena/bot/
COPY --from=runner /sdk/ ./src/main/java/arena/sdk/
RUN printf '<settings><mirrors><mirror><id>arena</id><url>%s</url><mirrorOf>*</mirrorOf></mirror></mirrors></settings>\n' \
       "$ARENA_MAVEN_MIRROR_URL" > /tmp/arena-settings.xml \
    && mvn --batch-mode --no-transfer-progress --settings /tmp/arena-settings.xml -DskipTests package

FROM maven:3.9.9-eclipse-temurin-17-noble@sha256:578027eef9bd2191bad0fa011e0a352b1b92b883e5ec4ac30d1a96d9c3f3e9b5
ENV BOT_HOST=0.0.0.0 BOT_PORT=8080
WORKDIR /bot
COPY . .
COPY --from=build /src/target/bot.jar /opt/bot.jar
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=65.0", "-cp", "/opt/bot.jar", "arena.bot.Main"]
