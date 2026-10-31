# -*- coding: utf-8 -*-
"""Copia a datos/ lo que el contenedor necesita de las semanas 7 y 9. Solo lee esas carpetas; nunca las modifica.

    python scripts/preparar_datos.py
"""
import json
import os
import shutil
import sys

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(AQUI))
DESTINO = os.path.join(AQUI, "datos")
ARCHIVOS = [("Semana 7/grafo_glaciares/nodos.csv", "nodos.csv"),
            ("Semana 7/grafo_glaciares/relaciones.csv", "relaciones.csv"),
            ("Semana 9/G8_rag_glaciares/embeddings.npy", "embeddings.npy"),
            ("Semana 9/G8_rag_glaciares/embeddings_info.json", "embeddings_info.json"),
            ("Semana 9/G8_rag_glaciares/fragmentos_rag.parquet", "fragmentos_rag.parquet")]


def main():
    os.makedirs(DESTINO, exist_ok=True)
    faltan = [o for o, _ in ARCHIVOS if not os.path.exists(os.path.join(REPO, o))]
    if faltan:
        sys.exit("Faltan archivos en el repositorio: " + ", ".join(faltan))
    for origen, nombre in ARCHIVOS:
        o, d = os.path.join(REPO, origen), os.path.join(DESTINO, nombre)
        if not os.path.exists(d) or os.path.getsize(d) != os.path.getsize(o):
            shutil.copy2(o, d)
        print(f"{nombre:<26} {os.path.getsize(d) / 1e6:7.1f} MB")
    info = json.load(open(os.path.join(DESTINO, "embeddings_info.json"), encoding="utf-8"))
    print(f"Listo: {info['n']:,} fragmentos ({info['modelo']}) en {DESTINO}")


if __name__ == "__main__":
    main()
