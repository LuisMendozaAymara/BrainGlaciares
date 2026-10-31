# -*- coding: utf-8 -*-
"""Acceso al grafo para producción: Neo4j si está disponible; si no, la copia embebida en Kùzu y, en último caso, pandas.

Aura Free se pausa sola tras 72 horas sin escrituras, así que el respaldo no es teórico: sin él, el servicio desplegado
dejaría de responder en cuanto la instancia se pause. Si Neo4j cae con el servicio en marcha, se cambia al respaldo
sin perder la pregunta en curso y cada cierto tiempo se vuelve a probar Neo4j.
"""
import os
import re
import tempfile
import threading
import time

import graphrag_glaciares as GR

BASE_PREVIA = "kuzu_glaciares"


class GrafoNeo4jSeguro(GR.GrafoNeo4j):
    """Igual que el de la semana 10, con tiempo máximo por consulta y sesiones de solo lectura."""

    def __init__(self, uri, usuario, clave_, base=None, timeout=15):
        from neo4j import GraphDatabase
        # reintentos cortos: si la base cayó, es mejor pasar pronto a la copia que dejar esperando a quien preguntó
        self.driver = GraphDatabase.driver(uri, auth=(usuario, clave_), connection_timeout=5,
                                           connection_acquisition_timeout=10, max_connection_lifetime=1800,
                                           liveness_check_timeout=30, max_transaction_retry_time=3)
        self.driver.verify_connectivity()
        self.base, self.timeout = base, timeout
        self.nombre = "Neo4j"

    def q(self, cypher, **params):
        from neo4j import Query, RoutingControl
        extra = {"database_": self.base} if self.base else {}
        recs, _, keys = self.driver.execute_query(Query(cypher, timeout=self.timeout), params,
                                                  routing_=RoutingControl.READ, **extra)
        return GR.pd.DataFrame([r.data() for r in recs], columns=keys)


class GrafoKuzuSeguro(GR.GrafoKuzu):
    """Copia embebida: una conexión compartida protegida por un candado y con tiempo máximo por consulta.
    Si la imagen trae la base ya construida (datos/kuzu_glaciares), se abre en segundos y en solo lectura."""

    def __init__(self, carpeta, ruta_db=None, log=print, timeout=15):
        previa = os.path.join(carpeta, BASE_PREVIA)
        if ruta_db is None and os.path.exists(previa):
            import kuzu
            GR.GrafoLocal.__init__(self, carpeta)
            self.c = kuzu.Connection(kuzu.Database(previa, read_only=True))
            self.nombre = "Kùzu local (copia del grafo)"
        else:
            super().__init__(carpeta, ruta_db=ruta_db or os.path.join(tempfile.gettempdir(), f"kuzu_glaciares_{os.getpid()}"), log=log)
        self._candado = threading.RLock()
        self.c.set_query_timeout(timeout * 1000)

    def q(self, cypher, **params):
        with self._candado:
            return super().q(cypher, **params)


def _caidas():
    """Errores que indican que Neo4j no está disponible (no los de una consulta mal escrita)."""
    from neo4j.exceptions import AuthError, DatabaseUnavailable, ServiceUnavailable, SessionExpired
    return ServiceUnavailable, SessionExpired, DatabaseUnavailable, AuthError, OSError


class GrafoResiliente:
    """Misma interfaz que los grafos de la semana 10; decide en cada llamada cuál usar."""

    def __init__(self, cfg, log=print):
        self.cfg, self.log = cfg, log
        self._principal, self._respaldo = None, None
        self._caido_desde, self._ultimo_error = None, None
        self._candado = threading.RLock()
        self._conectar_principal()
        if self._principal is None:
            self._crear_respaldo()

    # ---------- conexión ----------
    def _conectar_principal(self):
        if not (self.cfg.neo4j_uri and self.cfg.neo4j_clave):
            self._ultimo_error = "Neo4j sin configurar"
            return
        try:
            self._principal = GrafoNeo4jSeguro(self.cfg.neo4j_uri, self.cfg.neo4j_usuario, self.cfg.neo4j_clave,
                                               self.cfg.neo4j_base, self.cfg.timeout_cypher)
            self._caido_desde, self._ultimo_error = None, None
            self.log("Grafo principal: Neo4j conectado")
        except Exception as e:
            self._principal, self._caido_desde = None, time.time()
            self._ultimo_error = _sin_direcciones(str(e))[:200]
            self.log(f"Neo4j no respondió ({self._ultimo_error}); se usa la copia embebida")

    def _crear_respaldo(self):
        with self._candado:
            if self._respaldo is not None:
                return self._respaldo
            carpeta = self.cfg.datos_dir
            try:
                self._respaldo = GrafoKuzuSeguro(carpeta, log=self.log, timeout=self.cfg.timeout_cypher)
            except Exception as e:
                self.log(f"Kùzu no disponible ({str(e)[:160]}); solo fichas y caminos con pandas")
                self._respaldo = GR.GrafoLocal(carpeta)
            return self._respaldo

    def _actual(self):
        if self._principal is not None and self._caido_desde is None:
            return self._principal
        if self.cfg.neo4j_uri and self.cfg.neo4j_clave and self._caido_desde is not None \
                and time.time() - self._caido_desde > self.cfg.reintento_neo4j:
            with self._candado:
                if self._principal is None:
                    self._conectar_principal()
                else:
                    self._caido_desde = None   # se prueba con la próxima consulta
            if self._principal is not None and self._caido_desde is None:
                return self._principal
            self._caido_desde = time.time()
        return self._crear_respaldo()

    def _llamar(self, metodo, *a, **k):
        g = self._actual()
        try:
            r = getattr(g, metodo)(*a, **k)
            if g is self._principal:
                self._ultimo_error = None
            return r
        except _caidas() as e:
            if g is not self._principal:
                raise
            self._caido_desde, self._ultimo_error = time.time(), _sin_direcciones(str(e))[:200]
            self.log(f"Neo4j dejó de responder ({self._ultimo_error}); se cambia a la copia embebida")
            return getattr(self._crear_respaldo(), metodo)(*a, **k)

    # ---------- misma interfaz que GrafoNeo4j / GrafoKuzu ----------
    def q(self, cypher, **params):
        return self._llamar("q", cypher, **params)

    def catalogo(self):
        return self._llamar("catalogo")

    def documentos(self, ids):
        return self._llamar("documentos", ids)

    def ficha(self, ids, limite=30):
        return self._llamar("ficha", ids, limite)

    def camino(self, a, b, max_saltos=5):
        return self._llamar("camino", a, b, max_saltos)

    def aristas_entre(self, ks, limite=250):
        return self._llamar("aristas_entre", ks, limite)

    def esquema_texto(self):
        return self._llamar("esquema_texto")

    @property
    def nombre(self):
        return self._actual().nombre

    @property
    def cypher_ok(self):
        return self._actual().cypher_ok

    @property
    def cypher_libre_ok(self):
        return getattr(self._actual(), "cypher_libre_ok", True)

    def estado(self):
        return {"activo": self.nombre, "neo4j_configurado": bool(self.cfg.neo4j_uri and self.cfg.neo4j_clave),
                "neo4j_conectado": self._principal is not None and self._caido_desde is None,
                "ultimo_error": self._ultimo_error}

    def cerrar(self):
        if self._principal is not None:
            self._principal.cerrar()


def _sin_direcciones(t):
    """Los mensajes de error pueden traer la dirección de la base: no deben llegar a registros públicos ni al navegador."""
    t = re.sub(r"\b[\w+.-]+://\S+", "<dirección>", t)
    return re.sub(r"\b[\w.-]+:\d{2,5}\b", "<dirección>", t)


def construir_base_previa(carpeta, log=print):
    """Construye datos/kuzu_glaciares para abrirla luego en solo lectura. Se ejecuta al crear la imagen."""
    g = GR.GrafoKuzu(carpeta, ruta_db=os.path.join(carpeta, BASE_PREVIA), log=log)
    g.c.execute("CHECKPOINT")
    g.c.close()
