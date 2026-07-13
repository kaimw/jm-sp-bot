FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=local \
    DATABASE_URL=sqlite:///data/app.db \
    ADMIN_USERNAME=admin \
    ADMIN_PASSWORD=admin \
    AUTH_SECRET=change-this-local-secret \
    AUTH_SESSION_SECONDS=28800

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY scripts ./scripts

# 安装 Python 依赖（非 editable 模式，避免 flat-layout 问题）
RUN pip install --no-cache-dir fastapi uvicorn httpx sqlalchemy python-multipart \
    openpyxl python-docx pypdf psycopg[binary] pydantic "uvicorn[standard]"

# 创建数据目录
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
