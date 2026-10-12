# 备用构建方式：直接把项目目录当构建上下文
#   cd bhu_campus_agent
#   docker build -t campus-agent .
# 与上一级 /project/dockerfile 的效果完全一致，二选一即可。
FROM python:3.11-slim

WORKDIR /app

# 让 print() 的日志立刻输出到 docker logs（否则 [init] 那几行会被 stdout 缓冲区吞掉），
# 同时固定 UTF-8 编码，避免中文日志乱码
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

RUN sed -i 's@http://deb.debian.org@https://mirrors.aliyun.com@g; s@http://security.debian.org@https://mirrors.aliyun.com@g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's@deb.debian.org@mirrors.aliyun.com@g; s@security.debian.org@mirrors.aliyun.com@g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update

RUN PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    python -m playwright install --with-deps chromium

COPY . /app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
