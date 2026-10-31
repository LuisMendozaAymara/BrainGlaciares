# -*- coding: utf-8 -*-
"""Cadena de LLM para producción, con herramientas y a salvo de varios hilos.

Si un modelo falla se pasa al siguiente en la misma llamada. El que agotó su cuota queda "en descanso" un tiempo
(cortacircuitos), para que las preguntas siguientes no pierdan segundos insistiendo con él.
"""
import re
import threading
import time

from langchain_core.messages import AIMessage

DESCANSO_ERROR = 30         # segundos tras un error cualquiera
DESCANSO_MINUTO = 60        # límite por minuto
DESCANSO_DIARIO = 3600      # límite diario
ESPERA_BREVE = 15           # si todos descansan, segundos que vale la pena esperar al primero que vuelva


class SinModelos(RuntimeError):
    """Ningún LLM pudo responder (el mensaje no menciona cuotas, para que la semana 10 no se quede esperando)."""


def crear_modelo(proveedor, modelo, clave, timeout=30):
    if proveedor == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=modelo, api_key=clave, temperature=0, timeout=timeout, max_retries=0)
    if proveedor == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=modelo, google_api_key=clave, temperature=0, timeout=timeout, retries=1)
    raise ValueError(f"Proveedor desconocido: {proveedor}")


def eslabones_desde_config(cfg):
    """Convierte "groq:modelo,gemini:modelo" en [(nombre, modelo)]; se omiten los proveedores sin clave."""
    claves = {"groq": cfg.groq_clave, "gemini": cfg.gemini_clave}
    eslabones = []
    for item in (x.strip() for x in cfg.modelos.split(",")):
        proveedor, _, modelo = item.partition(":")
        if modelo and claves.get(proveedor):
            eslabones.append((f"{proveedor} · {modelo}", crear_modelo(proveedor, modelo, claves[proveedor], cfg.timeout_llm)))
    return eslabones


def descanso_para(error):
    t = str(error)
    # lo que dice el proveedor manda: "try again in 7.5s", "500ms", "1m30.5s", "2h5m"
    m = re.search(r"(?:try again in|retry in)\s*(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)(ms|s))?", t, re.I)
    if m and any(m.group(i) for i in (1, 2, 3)):
        s = 3600 * int(m.group(1) or 0) + 60 * int(m.group(2) or 0)
        if m.group(3):
            s += float(m.group(3)) / (1000 if m.group(4).lower() == "ms" else 1)
        return min(max(s, 1.0) + 1, DESCANSO_DIARIO)
    if re.search(r"per day|TPD|RPD|daily|exhausted|billing|insufficient", t, re.I):
        return DESCANSO_DIARIO
    if re.search(r"rate|429|quota|limit", t, re.I):
        return DESCANSO_MINUTO
    return DESCANSO_ERROR


def texto_de(mensaje):
    """Texto de un AIMessage (algunos modelos devuelven una lista de partes o razonamiento entre <think>)."""
    c = getattr(mensaje, "content", mensaje)
    if isinstance(c, list):
        c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return re.sub(r"<think>.*?</think>", "", c or "", flags=re.S).strip()


class CadenaModelos:
    def __init__(self, eslabones, log=print, reloj=time.time, dormir=time.sleep):
        self.eslabones = list(eslabones)
        self.log, self.reloj, self.dormir = log, reloj, dormir
        self._candado = threading.Lock()
        self._hasta = {}              # nombre del modelo → hasta cuándo descansa
        self._con_herramientas = {}   # (nombre, herramientas) → modelo con las herramientas enlazadas
        self.uso, self.errores = {}, {}
        self._hilo = threading.local()

    @property
    def ultimo(self):
        """Modelo que respondió la última llamada de este hilo."""
        return getattr(self._hilo, "ultimo", None)

    def _modelo(self, nombre, modelo, herramientas):
        if not herramientas:
            return modelo
        clave = (nombre, id(herramientas))
        with self._candado:
            if clave not in self._con_herramientas:
                self._con_herramientas[clave] = modelo.bind_tools(herramientas)
            return self._con_herramientas[clave]

    def _esperar_si_es_breve(self, fecha_limite):
        """Si todos descansan pero uno vuelve en segundos (límite por minuto), conviene esperarlo antes que dejar de redactar."""
        if not self.eslabones:
            return
        ahora = self.reloj()
        pronto = min(self._hasta.get(n, 0) for n, _ in self.eslabones)
        if ahora < pronto <= ahora + ESPERA_BREVE and (fecha_limite is None or pronto < fecha_limite):
            self.dormir(pronto - ahora)

    def invocar(self, mensajes, herramientas=None, fecha_limite=None):
        """Devuelve el AIMessage del primer modelo disponible. Sin modelos disponibles lanza SinModelos."""
        ultimo_error = "sin modelos configurados" if not self.eslabones else "todos en descanso"
        self._esperar_si_es_breve(fecha_limite)
        ahora = self.reloj()
        disponibles = [e for e in self.eslabones if self._hasta.get(e[0], 0) <= ahora]
        for nombre, modelo in disponibles:
            if fecha_limite is not None and self.reloj() >= fecha_limite:
                ultimo_error = "se acabó el tiempo de la pregunta"
                break
            try:
                r = self._modelo(nombre, modelo, herramientas).invoke(mensajes)
                if not isinstance(r, AIMessage):
                    r = AIMessage(content=str(getattr(r, "content", r)))
                if not texto_de(r) and not r.tool_calls:
                    raise ValueError("respuesta vacía")
                with self._candado:
                    self.uso[nombre] = self.uso.get(nombre, 0) + 1
                self._hilo.ultimo = nombre
                return r
            except Exception as e:
                descanso = descanso_para(e)
                with self._candado:
                    self._hasta[nombre] = self.reloj() + descanso
                    self.errores[nombre] = self.errores.get(nombre, 0) + 1
                ultimo_error = f"{nombre}: {type(e).__name__}"
                self.log(f"LLM {nombre} falló ({type(e).__name__}: {re.sub(r'org_[A-Za-z0-9]+', 'org', str(e))[:320]}); descansa {descanso:.0f} s")
        raise SinModelos(ultimo_error)

    # interfaz simple para el motor de la semana 10 (texto sin herramientas)
    def invoke(self, mensajes):
        return self.invocar(mensajes)

    def estado(self):
        ahora = self.reloj()
        return [{"modelo": n, "disponible": self._hasta.get(n, 0) <= ahora, "usos": self.uso.get(n, 0),
                 "errores": self.errores.get(n, 0)} for n, _ in self.eslabones]
