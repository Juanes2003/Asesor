FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 DATA_DIR=/data
WORKDIR /app
RUN groupadd --system asesor && useradd --system --gid asesor asesor && install -d -o asesor -g asesor -m 0700 /data
COPY --chown=asesor:asesor app.py estado.py respuestas.py servidor.py ./
USER asesor
VOLUME /data
EXPOSE 8080
CMD ["python", "app.py"]
