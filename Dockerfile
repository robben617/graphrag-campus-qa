# 校园知识问答系统 —— 后端应用镜像

FROM python:3.12-slim

WORKDIR /app

# 依赖先行（利用 Docker 层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码与公开样例
COPY app ./app
COPY demo ./demo
COPY frontend ./frontend

# 本地模型（体积大，通常挂载卷提供；如内置于镜像则取消下一行注释）
# COPY local_models ./local_models

ENV PYTHONUNBUFFERED=1 \
    SCHOOL_DB_PATH=/app/runtime/demo.db \
    SCHOOL_DATA_DIR=/app/demo

EXPOSE 8000

CMD ["python", "-m", "app.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
