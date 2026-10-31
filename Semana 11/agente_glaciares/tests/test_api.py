# -*- coding: utf-8 -*-
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api import Limitador, crear_app
from config import Config
from servicio import CacheRespuestas, Memoria

RESULTADO = {"pregunta": "p", "respuesta": "Palcacocha [1]", "modo": "agente", "modelo": "falso", "autocorregida": False, "avisos": [],
             "fuentes": [{"n": 1, "tipo": "grafo", "texto": "t", "referencia": "r", "documento": "20.500.12748/608", "pagina": "68", "evidencia": "e", "citada": True, "url": "https://hdl.handle.net/x"}],
             "citadas": [1], "n_citas": 1, "n_citas_grafo": 1, "n_citas_texto": 0, "docs_citados": ["x"], "instituciones": ["INAIGEM"],
             "herramientas": ["consulta_verificada"], "pasos": [], "contextos": ["secreto interno"], "segundos": 0.1}


class ServicioFalso:
    estado, detalle, grafo = "listo", "ok", None

    def __init__(self, espera=0):
        self.espera, self.recibidas = espera, []

    def iniciar(self):
        pass

    def en_pasos(self, pregunta, sesion=None):
        self.recibidas.append((pregunta, sesion))
        time.sleep(self.espera)
        yield {"tipo": "paso", "paso": {"tipo": "herramienta", "herramienta": "ficha_entidad", "args": {}}}
        yield {"tipo": "final", "resultado": {**RESULTADO, "pregunta": pregunta}}

    def responder(self, pregunta, sesion=None):
        return list(self.en_pasos(pregunta, sesion))[-1]["resultado"]

    def info(self):
        return {"estado": self.estado, "detalle": self.detalle}


def cliente(srv=None, **config):
    srv = srv or ServicioFalso()
    return TestClient(crear_app(Config(**config), srv, cargar=False)), srv


def test_salud_siempre_responde_y_listo_indica_carga():
    c, srv = cliente()
    assert c.get("/salud").status_code == 200 and c.get("/listo").status_code == 200
    srv.estado = "cargando"
    assert c.get("/salud").status_code == 200 and c.get("/listo").status_code == 503


def test_pregunta_mientras_carga_pide_reintentar():
    c, srv = cliente()
    srv.estado = "cargando"
    r = c.post("/api/preguntar", json={"pregunta": "hola que tal"})
    assert r.status_code == 503 and r.headers["retry-after"]


def test_pregunta_ok_sin_contextos_internos():
    c, _ = cliente()
    r = c.post("/api/preguntar", json={"pregunta": "¿Qué riesgo tiene Palcacocha?", "sesion_id": "abcd1234"})
    d = r.json()
    assert r.status_code == 200 and d["respuesta"] == "Palcacocha [1]" and d["id"]
    assert "contextos" not in d


@pytest.mark.parametrize("cuerpo", [{}, {"pregunta": ""}, {"pregunta": "x"}, {"pregunta": "a" * 2000}, {"pregunta": "hola", "sesion_id": "../../etc"}, {"pregunta": 5}])
def test_entrada_invalida(cuerpo):
    c, srv = cliente(max_pregunta=1000)
    assert c.post("/api/preguntar", json=cuerpo).status_code == 422
    assert not srv.recibidas


def test_clave_de_api():
    c, _ = cliente(api_key="clave-larga")
    assert c.post("/api/preguntar", json={"pregunta": "hola que tal"}).status_code == 401
    assert c.post("/api/preguntar", json={"pregunta": "hola que tal"}, headers={"X-API-Key": "mala"}).status_code == 401
    assert c.post("/api/preguntar", json={"pregunta": "hola que tal"}, headers={"X-API-Key": "clave-larga"}).status_code == 200
    assert c.get("/salud").status_code == 200   # la plataforma no tiene la clave


def test_limite_por_ip():
    c, _ = cliente(limite_ip_min=2, limite_global_min=100)
    h = {"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}
    assert [c.post("/api/preguntar", json={"pregunta": "hola que tal"}, headers=h).status_code for _ in range(3)] == [200, 200, 429]
    assert c.post("/api/preguntar", json={"pregunta": "hola que tal"}, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_limite_global_frena_ip_falsificadas():
    c, _ = cliente(limite_ip_min=100, limite_global_min=2)
    codigos = [c.post("/api/preguntar", json={"pregunta": "hola que tal"}, headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(3)]
    assert codigos == [200, 200, 429]


def test_limitador_olvida_tras_un_minuto():
    t = [0.0]
    lim = Limitador(1, 10, reloj=lambda: t[0])
    assert lim.permitir("a") and not lim.permitir("a")
    t[0] = 61
    assert lim.permitir("a")


def test_stream_entrega_pasos_y_final():
    c, _ = cliente()
    r = c.post("/api/preguntar/stream", json={"pregunta": "hola que tal"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")]
    assert [e["tipo"] for e in evs] == ["inicio", "paso", "final"]
    assert "contextos" not in evs[-1]["resultado"]


def test_stream_con_error_interno_no_expone_detalles():
    class Roto(ServicioFalso):
        def en_pasos(self, pregunta, sesion=None):
            raise RuntimeError("clave=SECRETA")
            yield
    c, _ = cliente(Roto())
    evs = [json.loads(x[6:]) for x in c.post("/api/preguntar/stream", json={"pregunta": "hola que tal"}).text.split("\n\n") if x.startswith("data: ")]
    assert evs[-1]["tipo"] == "error" and "SECRETA" not in evs[-1]["detalle"]


def test_error_interno_generico():
    class Roto(ServicioFalso):
        def responder(self, pregunta, sesion=None):
            raise RuntimeError("clave=SECRETA")
    app = crear_app(Config(), Roto(), cargar=False)
    r = TestClient(app, raise_server_exceptions=False).post("/api/preguntar", json={"pregunta": "hola que tal"})
    assert r.status_code == 500 and "SECRETA" not in r.text


def test_el_turno_se_libera_tras_un_error():
    class Roto(ServicioFalso):
        def responder(self, pregunta, sesion=None):
            raise RuntimeError("x")
    app = crear_app(Config(max_concurrencia=1), Roto(), cargar=False)
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/preguntar", json={"pregunta": "hola que tal"})
    assert app.state.turnos.acquire(blocking=False)


def test_ocupado_cuando_no_hay_turnos(monkeypatch):
    import api
    monkeypatch.setattr(api, "ESPERA_TURNO", 0.1)
    app = crear_app(Config(max_concurrencia=1), ServicioFalso(), cargar=False)
    assert app.state.turnos.acquire()
    r = TestClient(app).post("/api/preguntar", json={"pregunta": "hola que tal"})
    assert r.status_code == 503


def test_varias_preguntas_a_la_vez():
    c, srv = cliente(ServicioFalso(espera=0.05), max_concurrencia=2, limite_ip_min=50, limite_global_min=50)
    codigos = []
    hilos = [threading.Thread(target=lambda: codigos.append(c.post("/api/preguntar", json={"pregunta": "hola que tal"}).status_code)) for _ in range(6)]
    [h.start() for h in hilos]
    [h.join() for h in hilos]
    assert codigos == [200] * 6


def test_pagina_y_cabeceras_de_seguridad():
    c, _ = cliente()
    r = c.get("/")
    assert r.status_code == 200 and "Agente Glaciares" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/estilos.css").status_code == 200
    assert "content-security-policy" not in c.get("/docs").headers


def test_sin_cors_por_defecto():
    c, _ = cliente()
    assert "access-control-allow-origin" not in c.get("/salud", headers={"Origin": "https://malo.example"}).headers


# ---------- memoria y caché ----------
def test_memoria_quita_citas_y_limita_turnos():
    m = Memoria(turnos=2)
    for i in range(3):
        m.guardar("s", f"p{i}", f"r{i} [1][2]")
    assert m.historial("s") == [("p1", "r1"), ("p2", "r2")]
    assert m.historial(None) == [] and m.historial("otra") == []


def test_memoria_caduca_y_tiene_tope():
    t = [0.0]
    m = Memoria(sesiones=2, ttl=10, reloj=lambda: t[0])
    m.guardar("a", "p", "r")
    m.guardar("b", "p", "r")
    m.guardar("c", "p", "r")
    assert m.historial("a") == [] and m.historial("b")
    t[0] = 11
    assert m.historial("b") == []


def test_env_con_bom_comentarios_y_comillas(tmp_path, monkeypatch):
    from config import cargar_env
    ruta = tmp_path / ".env"
    ruta.write_text('﻿CLAVE_UNO=abc\n# COMENTADA=x\nCLAVE_DOS="d=e"\nCLAVE_TRES=archivo\n', encoding="utf-8")
    for k in ("CLAVE_UNO", "COMENTADA", "CLAVE_DOS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAVE_TRES", "entorno")
    cargar_env(str(ruta))
    assert os.environ["CLAVE_UNO"] == "abc" and os.environ["CLAVE_DOS"] == "d=e"
    assert "COMENTADA" not in os.environ and os.environ["CLAVE_TRES"] == "entorno"   # el entorno manda
    for k in ("CLAVE_UNO", "CLAVE_DOS"):
        monkeypatch.delenv(k)


def test_un_no_encontre_no_queda_en_cache():
    from servicio import Servicio

    class AgenteFalso:
        def __init__(self, citas):
            self.citas = citas

        def en_pasos(self, pregunta, historial=None):
            yield {"tipo": "final", "resultado": {**RESULTADO, "pregunta": pregunta, "n_citas": self.citas}}

    srv = Servicio(Config())
    srv.agente = AgenteFalso(0)
    srv.responder("lagunas de cusco")
    assert srv.cache.obtener("lagunas de cusco") is None
    srv.agente = AgenteFalso(3)
    srv.responder("lagunas de cusco")
    assert srv.cache.obtener("lagunas de cusco") is not None


def test_cache_ignora_tildes_y_mayusculas():
    c = CacheRespuestas()
    c.guardar("¿Qué riesgo tiene Parón?", {"r": 1})
    assert c.obtener("¿que riesgo tiene PARON?") == {"r": 1}
