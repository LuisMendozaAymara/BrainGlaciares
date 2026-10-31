# Agente con herramientas · imagen única para cualquier nube que corra contenedores (Azure Container Apps, Cloud Run…)
# Antes de construir: python scripts/preparar_datos.py    (copia los datos de las semanas 7 y 9 a datos/)
# Se construye sola en GitHub Actions (.github/workflows/agente-imagen.yml) o en tu computador (reserva 15 GB):
#   docker build -t agente-glaciares .
#   docker run --env-file .env -p 8080:8080 agente-glaciares

# --- 1. Dependencias y modelo de embeddings: capas que casi nunca cambian ---
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/opt/modelos \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2
WORKDIR /app
COPY requirements.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu \
    && pip install -r requirements.txt
# El modelo queda dentro de la imagen: al despertar, el servicio no depende de descargarlo (2.3 GB).
# Sin ONNX ni los pesos de ColBERT y sparse, que este proyecto no usa: serían 2.3 GB más.
# Con --build-arg HORNEAR_MODELO=0 se descarga al arrancar o se usa EMBEDDER=ninguno (solo BM25).
ARG HORNEAR_MODELO=1
RUN if [ "$HORNEAR_MODELO" = "1" ]; then \
        python -c "from huggingface_hub import snapshot_download as d; d('BAAI/bge-m3', ignore_patterns=['onnx/*', 'imgs/*', '*.pt'])"; \
    fi

# --- 2. Entorno de pruebas: docker build --target pruebas -t agente-pruebas . ---
FROM base AS pruebas
RUN pip install pytest httpx

# --- 3. Producción ---
FROM base AS produccion
COPY *.py ./
COPY static ./static
COPY datos ./datos
# La copia del grafo queda lista: si Neo4j está en pausa al despertar, el servicio no pierde 2 minutos construyéndola
RUN python -c "from grafo_resiliente import construir_base_previa as c; c('datos')"
RUN useradd --uid 1000 --create-home agente && chown -R agente /app
USER agente
ENV PORT=8080 HF_HUB_OFFLINE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
    CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/salud', timeout=4)"
# Un solo proceso: los modelos y los índices viven en memoria; la concurrencia la dan los hilos de FastAPI.
CMD ["sh", "-c", "exec uvicorn api:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
