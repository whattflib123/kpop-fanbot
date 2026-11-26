FROM python:3.12-slim
WORKDIR /app
# Claude Code CLI：translate.py 的 cli 後端用（訂閱額度翻譯，見 README）
# ffmpeg：live_summary.py 量直播音量找聲音峰值
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/root/.local/bin:$PATH
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 DB_PATH=/app/data/state.db
CMD ["python", "main.py"]
