# =============================================================================
# AI 科研论文工作台 —— 生产镜像
#
# 多阶段构建：
#   stage 1 (builder)：装编译依赖并把依赖装进 /opt/venv。
#       cryptography / uvloop / httptools 在没有 wheel 的平台上需要 gcc + libffi-dev，
#       放在 builder 里可以避免把编译工具链带进运行镜像。
#   stage 2 (runtime)：干净的 python:3.11-slim + 一个非 root 用户。
# =============================================================================
FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


FROM python:3.11-slim

# curl 用于容器健康检查
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765

WORKDIR /app
COPY app ./app

# 非 root 运行，避免把挂载的 ./data 文件改成 root 属主。
# uid/gid 默认 10001；若与宿主不一致，可在 docker-compose.yml 里用
# user: "${UID}:${GID}" 覆盖（deploy.sh 会自动带上当前用户的 UID/GID）。
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data

USER appuser
EXPOSE 8765

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8765}/api/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8765}"]
