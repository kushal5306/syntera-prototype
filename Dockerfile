# LibreDWG (GPLv3) provides dwg2dxf for DWG intake. It is built from the pinned, checksummed
# GNU release and runs as a separate program; Syntera never links against it.
FROM python:3.11-slim-bookworm AS libredwg

ARG LIBREDWG_VERSION=0.13.3
ARG LIBREDWG_SHA256=83f1f6e78a744777a481ff4520e4cef3f8ac4b2c1c25671077ca12fe81e8816e

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl --fail --location --silent --show-error \
        --output libredwg.tar.xz \
        "https://github.com/LibreDWG/libredwg/releases/download/${LIBREDWG_VERSION}/libredwg-${LIBREDWG_VERSION}.tar.xz" \
    && echo "${LIBREDWG_SHA256}  libredwg.tar.xz" | sha256sum --check - \
    && tar -xf libredwg.tar.xz \
    && cd "libredwg-${LIBREDWG_VERSION}" \
    && ./configure --prefix=/opt/libredwg --disable-bindings --disable-static \
    && make -j"$(nproc)" \
    && make install \
    && cp COPYING /opt/libredwg/COPYING

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/syntera \
    XDG_CACHE_HOME=/home/syntera/.cache \
    MPLCONFIGDIR=/home/syntera/.config/matplotlib

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libgl1 \
        libglu1-mesa \
        libsm6 \
        libxext6 \
        libxrender1 \
        calculix-ccx \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=libredwg /opt/libredwg /opt/libredwg
RUN ln -s /opt/libredwg/bin/dwg2dxf /usr/local/bin/dwg2dxf \
    && echo /opt/libredwg/lib > /etc/ld.so.conf.d/libredwg.conf \
    && ldconfig \
    && dwg2dxf --version

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install .

COPY examples ./examples

RUN groupadd --system syntera \
    && useradd --system --create-home --gid syntera --home-dir /home/syntera syntera \
    && mkdir -p /home/syntera/.cache/ezdxf /home/syntera/.config/matplotlib \
    && mkdir -p /app/outputs \
    && chown -R syntera:syntera /home/syntera /app/outputs

USER syntera

EXPOSE 8000
VOLUME ["/app/outputs"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"]

CMD ["python", "-m", "syntera.cli", "web", "--config", "examples/demo_skid.yaml", "--output", "outputs/web", "--host", "0.0.0.0", "--port", "8000"]
