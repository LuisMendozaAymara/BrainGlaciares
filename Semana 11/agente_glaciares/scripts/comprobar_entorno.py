# -*- coding: utf-8 -*-
"""Comprueba las claves del .env antes de desplegar: Neo4j y cada LLM, incluida su capacidad de llamar herramientas.

    python scripts/comprobar_entorno.py

Gasta una llamada mínima por modelo. Nunca muestra las claves.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import cargar_config  # noqa: E402
from grafo_resiliente import GrafoNeo4jSeguro, _sin_direcciones  # noqa: E402
from llm import eslabones_desde_config  # noqa: E402


def main():
    cfg = cargar_config()
    fallos = 0

    print("== Neo4j ==")
    if not (cfg.neo4j_uri and cfg.neo4j_clave):
        print("  sin configurar: se usará la copia embebida (Kùzu)")
    else:
        try:
            g = GrafoNeo4jSeguro(cfg.neo4j_uri, cfg.neo4j_usuario, cfg.neo4j_clave, cfg.neo4j_base, cfg.timeout_cypher)
            n = int(g.q("MATCH (n:Entidad) RETURN count(n) AS n").n.iloc[0])
            r = int(g.q("MATCH ()-[r]->() RETURN count(r) AS n").n.iloc[0])
            g.cerrar()
            print(f"  OK: {n:,} nodos · {r:,} relaciones")
        except Exception as e:
            print(f"  FALLA: {_sin_direcciones(str(e))[:200]}")
            print("  ¿Instancia en pausa? Reanúdala en console.neo4j.io. Si se borró: python scripts/cargar_neo4j.py")

    print("== LLM (texto y herramientas) ==")
    from langchain_core.tools import tool

    @tool
    def sumar(a: int, b: int) -> int:
        """Suma dos números."""
        return a + b

    eslabones = eslabones_desde_config(cfg)
    if not eslabones:
        print("  sin claves: el agente responderá en modo evidencia, sin redactar")
    con_herramientas = 0
    for nombre, modelo in eslabones:
        try:
            r = modelo.bind_tools([sumar]).invoke("Usa la herramienta sumar para calcular 2 + 3.")
            ok = bool(r.tool_calls)
            con_herramientas += ok
            print(f"  {'OK ' if ok else 'SIN'} {nombre}" + ("" if ok else ": respondió pero no llamó la herramienta"))
        except Exception as e:
            print(f"  FALLA {nombre}: {type(e).__name__}: {str(e)[:140]}")
    if eslabones and not con_herramientas:
        print("  Ningún modelo llamó herramientas: revisa las claves o cambia AGENTE_MODELOS.")
        fallos += 1
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
