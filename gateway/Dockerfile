FROM python:3.12-alpine
RUN adduser -D -u 10001 app
COPY kcresize-gateway.py /app/kcresize-gateway.py
USER 10001
EXPOSE 8088
HEALTHCHECK CMD wget -qO- http://127.0.0.1:8088/healthz || exit 1
ENTRYPOINT ["python3", "/app/kcresize-gateway.py"]
