# syntax=docker/dockerfile:1
# Android build-tools include x86_64 host executables; build with --platform linux/amd64.
FROM gradle:8.13-jdk17 AS build
USER root
ENV ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools \
    && curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip -o /tmp/sdk.zip \
    && unzip -q /tmp/sdk.zip -d ${ANDROID_HOME}/cmdline-tools \
    && mv ${ANDROID_HOME}/cmdline-tools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest \
    && rm /tmp/sdk.zip
ENV PATH="${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${PATH}"
RUN yes | sdkmanager --licenses >/dev/null
RUN sdkmanager "platforms;android-35" "build-tools;35.0.0" "platform-tools"
WORKDIR /project
COPY . .
RUN mkdir -p signing && if [ ! -s signing/debug.keystore ]; then \
    keytool -genkeypair -keystore signing/debug.keystore -storepass android -keypass android \
    -alias androiddebugkey -dname "CN=Android Debug,O=Android,C=US" -keyalg RSA -keysize 2048 -validity 10000; fi
RUN --mount=type=cache,target=/root/.gradle gradle --no-daemon :app:assembleDebug :app:testDebugUnitTest :app:lintDebug
RUN mkdir /out && cp app/build/outputs/apk/debug/app-debug.apk /out/AirFrame-android-debug.apk \
    && cp app/build/reports/lint-results-debug.html /out/android-lint.html

FROM scratch AS export
COPY --from=build /out/ /
