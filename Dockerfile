FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir watchdog

COPY entrypoint.sh app.py ./
RUN chmod +x entrypoint.sh

ENV HOST=0.0.0.0
ENV PORT=8000
ENV DOCKER_SOCKET=/var/run/docker.sock

EXPOSE 8000

CMD ["./entrypoint.sh"]
