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
    && rm -rf /var/lib/apt/lists/*

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
