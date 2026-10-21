# syntax=docker/dockerfile:1
FROM python:3.13-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install 'aiohttp>=3.13.5,<4' 'aiortc==1.15.0' 'setuptools>=69' build
COPY pyproject.toml ./
COPY airframe ./airframe
RUN pip install --no-deps --no-build-isolation .
COPY tests ./tests
RUN python -m unittest discover -s tests -v
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

FROM test AS wheel
RUN python -m build --no-isolation --wheel --outdir /out

FROM scratch AS export
COPY --from=wheel /out/ /

FROM python:3.13-slim AS portable
ARG DESKTOP_TARGET=macos-arm64
ENV PIP_NO_CACHE_DIR=1
RUN if [ "$DESKTOP_TARGET" = "windows-x64" ]; then \
    apt-get update && apt-get install -y --no-install-recommends gcc-mingw-w64-x86-64 \
    && rm -rf /var/lib/apt/lists/*; fi
COPY --from=wheel /out/ /out/
COPY scripts/package-portable.py scripts/windows-launcher.c /scripts/
COPY README.md /README.md
RUN --mount=type=cache,target=/root/.cache/pip env -u PIP_NO_CACHE_DIR python /scripts/package-portable.py --target "$DESKTOP_TARGET"

FROM scratch AS portable-export
COPY --from=portable /portable-out/ /

