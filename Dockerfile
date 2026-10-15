FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core gosu && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY source/requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-platform.txt
COPY source/ ./
COPY docker-entrypoint.sh /usr/local/bin/nextplay-entrypoint
RUN useradd --create-home nextplay && mkdir /data && chown nextplay:nextplay /data && chmod +x /usr/local/bin/nextplay-entrypoint
ENV PORT=8770
EXPOSE 8770
ENTRYPOINT ["/usr/local/bin/nextplay-entrypoint"]
