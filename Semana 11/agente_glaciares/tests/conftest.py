# -*- coding: utf-8 -*-
"""Las pruebas usan los datos reales (grafo en Kùzu, 30 mil fragmentos con BM25) y un LLM con guion: no gastan cuota ni necesitan claves."""
import os
import sys

import pytest
from langchain_core.messages import AIMessage

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import rag_ligero  # noqa: E402
from agente import Agente  # noqa: E402
from config import Config  # noqa: E402
from grafo_resiliente import BASE_PREVIA, GrafoKuzuSeguro, GrafoResiliente, construir_base_previa  # noqa: E402
from herramientas import CajaHerramientas, MotorAgente  # noqa: E402
from llm import CadenaModelos  # noqa: E402

DATOS = os.environ.get("DATOS_DIR", os.path.join(RAIZ, "datos"))


class LLMGuionado:
    """Devuelve las respuestas de su guion en orden; una Exception del guion se lanza."""

    def __init__(self, guion):
        self.guion, self.recibidos, self.con_herramientas = list(guion), [], 0

    def bind_tools(self, herramientas):
        self.con_herramientas += 1
        return self

    def invoke(self, mensajes):
        self.recibidos.append(mensajes)
        if not self.guion:
            raise AssertionError("el guion se acabó: el agente llamó al LLM más veces de lo esperado")
        r = self.guion.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def llamar(herramienta, **args):
    return {"name": herramienta, "args": args, "id": f"c{abs(hash((herramienta, str(args)))) % 10**6}", "type": "tool_call"}


def pide(*llamadas):
    return AIMessage(content="", tool_calls=list(llamadas))


@pytest.fixture(scope="session")
def cfg():
    return Config(datos_dir=DATOS, embedder="ninguno", timeout_pregunta=60, timeout_llm=5)


@pytest.fixture(scope="session", autouse=True)
def base_previa():
    """La misma base de solo lectura que trae la imagen; se construye una vez (datos/ no se sube a Git)."""
    if not os.path.exists(os.path.join(DATOS, BASE_PREVIA)):
        construir_base_previa(DATOS, log=lambda m: None)


@pytest.fixture(scope="session")
def grafo(cfg, base_previa):
    return GrafoResiliente(cfg, log=lambda m: None)


@pytest.fixture(scope="session")
def indice():
    return rag_ligero.cargar_indice(DATOS, "ninguno", 0.58, log=lambda m: None)


@pytest.fixture(scope="session")
def motor_base(grafo, indice):
    return MotorAgente(grafo, indice=indice, llm=None, R=rag_ligero, config={"REINTENTOS_LLM": 1}, log=lambda m: None)


@pytest.fixture(scope="session")
def caja(motor_base):
    return CajaHerramientas(motor_base)


@pytest.fixture
def armar(motor_base, caja, cfg):
    """Crea un agente con un LLM de guion (o varios) sobre los datos reales."""
    creados = []

    def _armar(*guiones, config=None):
        falsos = [LLMGuionado(g) for g in guiones]
        cadena = CadenaModelos([(f"falso{i}", f) for i, f in enumerate(falsos)], log=lambda m: None)
        motor_base.llm = cadena if falsos else None
        creados.append(cadena)
        return Agente(motor_base, caja, cadena, config or cfg, log=lambda m: None), falsos

    yield _armar
    motor_base.llm = None


@pytest.fixture(scope="session")
def kuzu_solo(base_previa):
    return GrafoKuzuSeguro(DATOS, log=lambda m: None)
