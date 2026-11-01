# -*- coding: utf-8 -*-
"""API y página web del agente.

    GET  /                       chat
    GET  /salud                  ¿el proceso vive? (para la plataforma; siempre 200 si el servidor corre)
    GET  /listo                  ¿puede responder preguntas? (503 mientras carga)
    GET  /api/info               grafo activo, modelos, herramientas
    POST /api/preguntar          respuesta completa en JSON
    POST /api/preguntar/stream   los pasos del agente según ocurren (Server-Sent Events)
    GET  /docs                   documentación interactiva
"""
import asyncio
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import CARPETA_BASE, cargar_config
from servicio import VERSION, Servicio

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("neo4j").setLevel(logging.ERROR)   # sus avisos de reintento traen la dirección de la base
registro = logging.getLogger("agente")
# Mosaicos del mapa: imagen satelital de Esri y relieve público Terrarium (Mapzen). El visor los pide desde un worker.
MOSAICOS = "https://server.arcgisonline.com https://s3.amazonaws.com"
CSP = (f"default-src 'self'; img-src 'self' data: blob: {MOSAICOS}; style-src 'self'; script-src 'self'; connect-src 'self' {MOSAICOS}; "
       "worker-src blob:; child-src blob:; base-uri 'none'; form-action 'none'")
ESPERA_TURNO = 20   # segundos que una pregunta espera turno antes de responder "ocupado"


def log_json(evento, **campos):
    registro.info(json.dumps({"evento": evento, "t": round(time.time(), 1), **campos}, ensure_ascii=False, default=str))


class Limitador:
    """Ventana deslizante de un minuto, por IP y total. Protege la cuota gratuita de los LLM frente a abusos."""

    def __init__(self, por_ip, total, reloj=time.time):
        self.por_ip, self.total, self.reloj = por_ip, total, reloj
        self._ips, self._todas = defaultdict(deque), deque()
        self._candado = threading.Lock()

    def permitir(self, ip):
        ahora = self.reloj()
        with self._candado:
            for cola in (self._ips[ip], self._todas):
                while cola and ahora - cola[0] > 60:
                    cola.popleft()
            if not self._ips[ip]:
                self._ips.pop(ip, None)
            if len(self._ips[ip]) >= self.por_ip or len(self._todas) >= self.total:
                return False
            self._ips[ip].append(ahora)
            self._todas.append(ahora)
            if len(self._ips) > 5000:   # muchas IP distintas: se olvidan las inactivas
                for k in [k for k, v in self._ips.items() if not v or ahora - v[-1] > 60]:
                    self._ips.pop(k, None)
            return True


class Pregunta(BaseModel):
    pregunta: str = Field(min_length=2, description="La pregunta en lenguaje natural.")
    sesion_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$",
                                     description="Identificador de la conversación, para preguntas de seguimiento.")


def publico(r):
    """Lo que ve el cliente: sin los bloques internos que recibió el LLM."""
    return {k: v for k, v in r.items() if k != "contextos"}


def crear_app(cfg=None, servicio=None, cargar=True):
    cfg = cfg or cargar_config()
    servicio = servicio or Servicio(cfg, log=lambda m: log_json("servicio", mensaje=m))
    limitador = Limitador(cfg.limite_ip_min, cfg.limite_global_min)
    turnos = threading.BoundedSemaphore(cfg.max_concurrencia)

    @asynccontextmanager
    async def vida(app):
        if cargar and cfg.carga_sincrona:   # Cloud Run frena la CPU sin peticiones activas: se carga antes de abrir el puerto
            await asyncio.to_thread(servicio.cargar)
        elif cargar:
            servicio.iniciar()
        yield
        if servicio.grafo is not None:
            servicio.grafo.cerrar()

    app = FastAPI(title="Agente Enterprise Brain Glaciares", version=VERSION, lifespan=vida,
                  description="Agente con herramientas sobre el grafo de conocimiento y los 398 documentos de glaciares del Perú.")
    app.state.servicio, app.state.limitador, app.state.turnos = servicio, limitador, turnos
    origenes = [o.strip() for o in cfg.origenes_cors.split(",") if o.strip()]
    if origenes:
        app.add_middleware(CORSMiddleware, allow_origins=origenes, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-API-Key"])

    @app.middleware("http")
    async def cabeceras(request: Request, call_next):
        respuesta = await call_next(request)
        respuesta.headers["X-Content-Type-Options"] = "nosniff"
        respuesta.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(("/docs", "/redoc")):   # /docs carga sus recursos desde una CDN
            respuesta.headers["Content-Security-Policy"] = CSP
        return respuesta

    @app.exception_handler(Exception)
    async def error_general(request: Request, exc: Exception):
        ident = uuid.uuid4().hex[:8]
        log_json("error", id=ident, ruta=request.url.path, tipo=type(exc).__name__, detalle=str(exc)[:300])
        return JSONResponse(status_code=500, content={"detail": f"Error interno. Referencia: {ident}"})

    def _ip(request):
        reenviada = request.headers.get("x-forwarded-for", "")
        return reenviada.split(",")[0].strip() or (request.client.host if request.client else "desconocida")

    def _admitir(request, p):
        if cfg.api_key and not hmac.compare_digest(request.headers.get("x-api-key", ""), cfg.api_key):
            raise HTTPException(401, "Falta la clave de la API o no es correcta (cabecera X-API-Key).")
        if len(p.pregunta) > cfg.max_pregunta:
            raise HTTPException(422, f"La pregunta supera los {cfg.max_pregunta} caracteres.")
        if servicio.estado == "error":
            raise HTTPException(503, "El agente no pudo iniciar. Revisa los registros del servidor.")
        if servicio.estado != "listo":
            raise HTTPException(503, "El agente está cargando; vuelve a intentar en un momento.", headers={"Retry-After": "20"})
        if not limitador.permitir(_ip(request)):
            raise HTTPException(429, "Demasiadas preguntas por minuto. Espera un momento.", headers={"Retry-After": "30"})
        return p.pregunta.strip()

    def _registrar(ident, r):
        log_json("respuesta", id=ident, pregunta=r["pregunta"][:200], modo=r["modo"], modelo=r["modelo"], segundos=r["segundos"],
                 herramientas=r["herramientas"], citas=r["n_citas"], avisos=len(r["avisos"]), cache=r.get("desde_cache", False))

    @app.get("/salud", tags=["estado"])
    def salud():
        return {"estado": "vivo", "agente": servicio.estado}

    @app.get("/listo", tags=["estado"])
    def listo():
        if servicio.estado != "listo":
            return JSONResponse(status_code=503, content={"estado": servicio.estado, "detalle": servicio.detalle})
        return {"estado": "listo"}

    @app.get("/api/info", tags=["estado"])
    def info():
        return servicio.info()

    @app.get("/api/mapa", tags=["estado"])
    def mapa():
        """Lagunas con ubicación validada y la capa de lagunas en formación, sin gastar LLM."""
        if servicio.estado != "listo":
            raise HTTPException(503, "El agente está cargando; vuelve a intentar en un momento.", headers={"Retry-After": "20"})
        return servicio.datos_mapa()

    @app.post("/api/preguntar", tags=["agente"])
    def preguntar(p: Pregunta, request: Request):
        pregunta = _admitir(request, p)
        if not turnos.acquire(timeout=ESPERA_TURNO):
            raise HTTPException(503, "El agente está ocupado con otras preguntas; intenta en unos segundos.", headers={"Retry-After": "15"})
        ident = uuid.uuid4().hex[:8]
        try:
            r = servicio.responder(pregunta, p.sesion_id)
        finally:
            turnos.release()
        _registrar(ident, r)
        return {"id": ident, **publico(r)}

    @app.post("/api/preguntar/stream", tags=["agente"])
    def preguntar_stream(p: Pregunta, request: Request):
        pregunta = _admitir(request, p)
        ident = uuid.uuid4().hex[:8]

        def sse(datos):
            return f"data: {json.dumps(datos, ensure_ascii=False, default=str)}\n\n"

        def eventos():
            # el turno se toma aquí dentro: si el cliente se va antes de empezar, no queda ocupado
            if not turnos.acquire(timeout=ESPERA_TURNO):
                yield sse({"tipo": "error", "detalle": "El agente está ocupado con otras preguntas; intenta en unos segundos."})
                return
            try:
                yield sse({"tipo": "inicio", "id": ident})
                for ev in servicio.en_pasos(pregunta, p.sesion_id):
                    if ev["tipo"] == "final":
                        _registrar(ident, ev["resultado"])
                        yield sse({"tipo": "final", "resultado": {"id": ident, **publico(ev["resultado"])}})
                    else:
                        yield sse(ev)
            except Exception as e:
                log_json("error", id=ident, tipo=type(e).__name__, detalle=str(e)[:300])
                yield sse({"tipo": "error", "detalle": f"Error interno. Referencia: {ident}"})
            finally:
                turnos.release()

        return StreamingResponse(eventos(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    estaticos = os.path.join(CARPETA_BASE, "static")
    app.mount("/static", StaticFiles(directory=estaticos), name="static")

    @app.get("/", include_in_schema=False)
    def inicio():
        return FileResponse(os.path.join(estaticos, "index.html"))

    return app


app = crear_app()
