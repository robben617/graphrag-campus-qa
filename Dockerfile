# 校园知识问答系统 —— 应用镜像（后端 REST + 前端静态站点）
# 第三阶段：容器化部署（§6 挑战版#18）

FROM python:3.12-slim

WORKDIR /app

# 依赖先行（利用 Docker 层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码与数据
COPY app ./app
COPY data ./data
COPY frontend ./frontend
COPY docs ./docs
COPY README.md ./

# 本地模型（体积大，通常挂载卷提供；如内置于镜像则取消下一行注释）
# COPY local_models ./local_models

ENV PYTHONUNBUFFERED=1 \
    SCHOOL_DB_PATH=/app/data/school_kb.db \
    SCHOOL_DATA_DIR=/app/data

EXPOSE 8000

# 启动前先幂等导入，再启动后端
CMD ["sh", "-c", "python -m app.cli import || true && python -m app.cli serve --host 0.0.0.0 --port 8000"]
