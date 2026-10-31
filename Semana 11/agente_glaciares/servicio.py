# -*- coding: utf-8 -*-
"""Arma todas las piezas del agente y guarda lo que se comparte entre preguntas: memoria de conversaciones y caché de respuestas."""
import re
import threading
import time
from collections import OrderedDict, deque

import graphrag_glaciares as GR
import rag_ligero
from agente import Agente
from grafo_resiliente import GrafoResiliente, _sin_direcciones
from herramientas import CajaHerramientas, MotorAgente
from llm import CadenaModelos, eslabones_desde_config


class Memoria:
    """Últimos turnos de cada conversación, con caducidad y un tope de conversaciones para no agotar la memoria del servidor."""

    def __init__(self, turnos=6, sesiones=500, ttl=3600, reloj=time.time):
        self.turnos, self.sesiones, self.ttl, self.reloj = turnos, sesiones, ttl, reloj
        self._d = OrderedDict()
        self._candado = threading.Lock()

    def historial(self, sesion):
        if not sesion:
            return []
        with self._candado:
            v = self._d.get(sesion)
            if v is None or self.reloj() - v[0] > self.ttl:
                self._d.pop(sesion, None)
                return []
            self._d.move_to_end(sesion)
            return list(v[1])

    def guardar(self, sesion, pregunta, respuesta):
        if not sesion:
            return
        # los [n] solo valen dentro de su pregunta: sin esto, el LLM citaría números de la evidencia anterior
        limpia = re.sub(r"\s*\[\d+\]", "", respuesta)[:800]
        with self._candado:
            _, cola = self._d.get(sesion, (0, deque(maxlen=self.turnos)))
            cola.append((pregunta, limpia))
            self._d[sesion] = (self.reloj(), cola)
            self._d.move_to_end(sesion)
            while len(self._d) > self.sesiones:
                self._d.popitem(last=False)


class CacheRespuestas:
    """Respuestas ya verificadas: repetir una pregunta en la demo no gasta cuota de los LLM."""

    def __init__(self, tamano=256, ttl=3600, reloj=time.time):
        self.tamano, self.ttl, self.reloj = tamano, ttl, reloj
        self._d = OrderedDict()
        self._candado = threading.Lock()
        self.aciertos = 0

    def obtener(self, pregunta):
        k = GR.norm(pregunta)
        with self._candado:
            v = self._d.get(k)
            if v is None or self.reloj() - v[0] > self.ttl:
                self._d.pop(k, None)
                return None
            self.aciertos += 1
            self._d.move_to_end(k)
            return v[1]

    def guardar(self, pregunta, resultado):
        with self._candado:
            self._d[GR.norm(pregunta)] = (self.reloj(), resultado)
            self._d.move_to_end(GR.norm(pregunta))
            while len(self._d) > self.tamano:
                self._d.popitem(last=False)


class Servicio:
    def __init__(self, cfg, log=print):
        self.cfg, self.log = cfg, log
        self.estado, self.detalle = "cargando", "iniciando"
        self.grafo = self.indice = self.cadena = self.motor = self.caja = self.agente = None
        self.memoria = Memoria(cfg.memoria_turnos, cfg.memoria_sesiones, cfg.memoria_ttl)
        self.cache = CacheRespuestas(cfg.cache_respuestas, cfg.memoria_ttl)
        self.iniciado = time.time()
        self.preguntas = 0

    def iniciar(self):
        """Carga en segundo plano: el servidor responde de inmediato y la plataforma no lo da por caído al despertar."""
        threading.Thread(target=self.cargar, name="carga-agente", daemon=True).start()

    def cargar(self):
        t0 = time.time()
        try:
            self.detalle = "conectando el grafo"
            self.grafo = GrafoResiliente(self.cfg, self.log)
            self.detalle = "cargando el índice de texto y el modelo de embeddings"
            try:
                self.indice = rag_ligero.cargar_indice(self.cfg.datos_dir, self.cfg.embedder, self.cfg.umbral_sim, self.log)
            except Exception as e:
                self.log(f"Sin índice de texto: {_sin_direcciones(str(e))[:200]}")
            self.detalle = "armando el agente"
            eslabones = eslabones_desde_config(self.cfg)
            self.cadena = CadenaModelos(eslabones, self.log)
            config_motor = {"REINTENTOS_LLM": 1}   # los reintentos y el paso a otro modelo los hace la cadena
            self.motor = MotorAgente(self.grafo, indice=self.indice, llm=self.cadena if eslabones else None, R=rag_ligero,
                                     config=config_motor, log=self.log)
            self.caja = CajaHerramientas(self.motor)
            self.agente = Agente(self.motor, self.caja, self.cadena, self.cfg, self.log)
            self.estado, self.detalle = "listo", f"cargado en {time.time() - t0:.0f} s"
            self.log(f"Agente listo en {time.time() - t0:.0f} s · grafo: {self.grafo.nombre} · modelos: {len(eslabones)}")
        except Exception as e:
            self.estado, self.detalle = "error", _sin_direcciones(str(e))[:300]
            self.log(f"No se pudo iniciar el agente: {self.detalle}")

    def en_pasos(self, pregunta, sesion=None):
        historial = self.memoria.historial(sesion)
        self.preguntas += 1
        if not historial:
            guardada = self.cache.obtener(pregunta)
            if guardada is not None:
                self.memoria.guardar(sesion, pregunta, guardada["respuesta"])
                yield {"tipo": "final", "resultado": {**guardada, "desde_cache": True}}
                return
        for ev in self.agente.en_pasos(pregunta, historial):
            if ev["tipo"] == "final":
                r = ev["resultado"]
                self.memoria.guardar(sesion, pregunta, r["respuesta"])
                # solo se reutiliza lo que el LLM redactó y pasó la verificación; no los modos degradados ni un "no encontré",
                # que podría deberse a una mala decisión del modelo y quedaría fijo una hora
                if not historial and r["modo"] == "agente" and not r["avisos"] and r["n_citas"] > 0:
                    self.cache.guardar(pregunta, r)
            yield ev

    def responder(self, pregunta, sesion=None):
        ultimo = None
        for ev in self.en_pasos(pregunta, sesion):
            ultimo = ev
        return ultimo["resultado"]

    def info(self):
        d = {"estado": self.estado, "detalle": self.detalle, "version": VERSION, "activo_hace_s": int(time.time() - self.iniciado),
             "preguntas": self.preguntas, "aciertos_cache": self.cache.aciertos}
        if self.estado != "listo":
            return d
        d["grafo"] = self.grafo.estado()
        d["texto"] = {"fragmentos": len(self.indice.frag) if self.indice is not None else 0,
                      "busqueda": ("híbrida (bge-m3 + BM25)" if self.indice.embedder is not None else "solo BM25") if self.indice is not None else "no disponible"}
        d["modelos"] = self.cadena.estado()
        d["herramientas"] = [{"nombre": t.name, "descripcion": t.description.split("\n")[0]} for t in self.caja.herramientas]
        return d


VERSION = "1.0-agente"
