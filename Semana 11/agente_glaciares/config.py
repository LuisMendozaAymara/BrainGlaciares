# -*- coding: utf-8 -*-
"""Configuración del agente: todo sale de variables de entorno (ver .env.example). Las claves nunca se escriben en el código."""
import os
from dataclasses import dataclass, field

MODELOS_POR_DEFECTO = "groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,gemini:gemini-flash-lite-latest,gemini:gemini-flash-latest"
CARPETA_BASE = os.path.dirname(os.path.abspath(__file__))


def _primera(env, *nombres):
    return next((env[n].strip() for n in nombres if env.get(n, "").strip()), None)


def _entero(env, nombre, defecto):
    try:
        return int(env.get(nombre, defecto))
    except (TypeError, ValueError):
        return defecto


def _real(env, nombre, defecto):
    try:
        return float(env.get(nombre, defecto))
    except (TypeError, ValueError):
        return defecto


@dataclass(frozen=True)
class Config:
    # grafo
    neo4j_uri: str | None = None
    neo4j_usuario: str = "neo4j"
    neo4j_clave: str | None = field(default=None, repr=False)
    neo4j_base: str | None = None
    timeout_cypher: int = 15              # segundos que puede tardar una consulta al grafo
    reintento_neo4j: int = 300            # tras una caída, cada cuánto se vuelve a probar Neo4j
    # LLM
    groq_clave: str | None = field(default=None, repr=False)
    gemini_clave: str | None = field(default=None, repr=False)
    modelos: str = MODELOS_POR_DEFECTO    # "proveedor:modelo" en orden de uso
    timeout_llm: int = 30
    # texto
    datos_dir: str = os.path.join(CARPETA_BASE, "datos")
    embedder: str = "BAAI/bge-m3"         # "ninguno" = solo BM25 (para servidores con poca memoria)
    umbral_sim: float = 0.58              # umbral "fuera del corpus" calibrado en la semana 9
    # agente
    max_iter: int = 5                     # rondas máximas de herramientas por pregunta
    max_llamadas_ronda: int = 4           # herramientas que puede pedir el LLM en una ronda
    timeout_pregunta: int = 120           # presupuesto total de una pregunta
    # memoria y caché
    memoria_turnos: int = 6
    memoria_sesiones: int = 500
    memoria_ttl: int = 3600
    cache_respuestas: int = 256
    # API
    api_key: str | None = field(default=None, repr=False)
    max_pregunta: int = 1000
    max_concurrencia: int = 4
    limite_ip_min: int = 12               # preguntas por minuto de cada IP
    limite_global_min: int = 40           # preguntas por minuto en total (protege la cuota gratuita de los LLM)
    origenes_cors: str = ""               # orígenes separados por comas; vacío = solo el mismo sitio
    carga_sincrona: bool = False          # True = cargar todo antes de abrir el puerto (necesario en Cloud Run)


def cargar_env(ruta=None):
    """Lee el archivo .env si existe. Lo ya definido en el entorno manda (en la nube, los secretos de la plataforma)."""
    ruta = ruta or os.path.join(CARPETA_BASE, ".env")
    if not os.path.exists(ruta):
        return
    for linea in open(ruta, encoding="utf-8-sig"):   # el Bloc de notas puede guardar con marca BOM
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def cargar_config(env=None):
    if env is None:
        cargar_env()
        env = os.environ
    return Config(
        neo4j_uri=_primera(env, "NEO4J_URI"),
        neo4j_usuario=_primera(env, "NEO4J_USERNAME", "NEO4J_USER") or "neo4j",
        neo4j_clave=_primera(env, "NEO4J_PASSWORD"),
        neo4j_base=_primera(env, "NEO4J_DATABASE"),
        timeout_cypher=_entero(env, "TIMEOUT_CYPHER", 15),
        reintento_neo4j=_entero(env, "REINTENTO_NEO4J", 300),
        groq_clave=_primera(env, "GROQ_API_KEY"),
        gemini_clave=_primera(env, "GOOGLE_API_KEY", "GEMINI_API_KEY"),
        modelos=_primera(env, "AGENTE_MODELOS") or MODELOS_POR_DEFECTO,
        timeout_llm=_entero(env, "TIMEOUT_LLM", 30),
        datos_dir=_primera(env, "DATOS_DIR") or os.path.join(CARPETA_BASE, "datos"),
        embedder=_primera(env, "EMBEDDER") or "BAAI/bge-m3",
        umbral_sim=_real(env, "UMBRAL_SIM", 0.58),
        max_iter=_entero(env, "MAX_ITER", 5),
        max_llamadas_ronda=_entero(env, "MAX_LLAMADAS_RONDA", 4),
        timeout_pregunta=_entero(env, "TIMEOUT_PREGUNTA", 120),
        memoria_turnos=_entero(env, "MEMORIA_TURNOS", 6),
        memoria_sesiones=_entero(env, "MEMORIA_SESIONES", 500),
        memoria_ttl=_entero(env, "MEMORIA_TTL", 3600),
        cache_respuestas=_entero(env, "CACHE_RESPUESTAS", 256),
        api_key=_primera(env, "AGENTE_API_KEY"),
        max_pregunta=_entero(env, "MAX_PREGUNTA", 1000),
        max_concurrencia=_entero(env, "MAX_CONCURRENCIA", 4),
        limite_ip_min=_entero(env, "LIMITE_IP_MIN", 12),
        limite_global_min=_entero(env, "LIMITE_GLOBAL_MIN", 40),
        origenes_cors=_primera(env, "CORS_ORIGENES") or "",
        carga_sincrona=(_primera(env, "CARGA_SINCRONA") or "").lower() in ("1", "true", "si", "sí"),
    )
