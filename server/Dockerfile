ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KNOWLEDGE_DATA=/data

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://security.debian.org/debian-security

RUN set -eu; \
    sed -i \
      -e "s|http://deb.debian.org/debian$|${DEBIAN_MIRROR}|" \
      -e "s|http://deb.debian.org/debian-security$|${DEBIAN_SECURITY_MIRROR}|" \
      -e "s|http://security.debian.org/debian-security$|${DEBIAN_SECURITY_MIRROR}|" \
      /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update; \
    DEBIAN_FRONTEND=noninteractive apt-get \
      -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 \
      install -y --no-install-recommends ffmpeg ca-certificates; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN python -m pip install --no-cache-dir --only-binary=:all: \
      --index-url "${PIP_INDEX_URL}" --timeout 30 --retries 2 \
      -r /app/requirements.txt

RUN useradd --uid 10001 --create-home worker \
    && mkdir /data && chown worker:worker /data

COPY __init__.py app.py processing.py taxonomy.py smoke_test.py /app/server/
USER worker
EXPOSE 8766
CMD ["python", "-m", "server.app"]
