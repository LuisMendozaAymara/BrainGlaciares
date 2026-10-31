# -*- coding: utf-8 -*-
"""Recarga el grafo en una instancia de Neo4j VACÍA desde datos/nodos.csv y datos/relaciones.csv.

Aura Free borra las instancias que pasan 30 días en pausa. Con este script, el grafo se recupera en una instancia nueva.
Solo agrega con MERGE; se niega a escribir si la base ya tiene nodos, salvo que se pase --permitir-existente.

    python scripts/cargar_neo4j.py          (lee NEO4J_URI, NEO4J_USERNAME y NEO4J_PASSWORD del entorno)
"""
import argparse
import math
import os
import re
import sys
import time

import pandas as pd

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTEROS = {"n_docs", "anio", "anio_imagenes", "anio_formacion", "n_poblados", "orden", "pagina", "n"}
REALES = {"valor", "confianza"}
LOGICOS = {"en_riesgo", "hidro", "grande", "dudoso"}
LISTAS = {"departamentos"}


def propiedades(fila, excluir):
    p = {}
    for k, v in fila.items():
        if k in excluir or v is None or (isinstance(v, float) and math.isnan(v)) or v == "":
            continue
        if k in ENTEROS:
            p[k] = int(float(v))
        elif k in REALES:
            p[k] = float(v)
        elif k in LOGICOS:
            p[k] = str(v).strip().lower() == "true"
        elif k in LISTAS:
            p[k] = [x for x in str(v).split(";") if x]
        else:
            p[k] = str(v)
    return p


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--datos", default=os.path.join(AQUI, "datos"))
    a.add_argument("--lote", type=int, default=1000)
    a.add_argument("--permitir-existente", action="store_true", help="agregar aunque la base ya tenga nodos")
    args = a.parse_args()
    uri, clave = os.environ.get("NEO4J_URI"), os.environ.get("NEO4J_PASSWORD")
    if not (uri and clave):
        sys.exit("Faltan NEO4J_URI y NEO4J_PASSWORD")
    from neo4j import GraphDatabase
    N = pd.read_csv(os.path.join(args.datos, "nodos.csv"), dtype=str, low_memory=False)
    R = pd.read_csv(os.path.join(args.datos, "relaciones.csv"), dtype=str, low_memory=False)
    t0 = time.time()
    with GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USERNAME", "neo4j"), clave)) as driver:
        driver.verify_connectivity()
        existentes = driver.execute_query("MATCH (n) RETURN count(n) AS n").records[0]["n"]
        if existentes and not args.permitir_existente:
            sys.exit(f"La base ya tiene {existentes:,} nodos. No se escribió nada.")
        for c in ["CREATE CONSTRAINT entidad_id IF NOT EXISTS FOR (n:Entidad) REQUIRE n.id IS UNIQUE",
                  "CREATE INDEX entidad_k IF NOT EXISTS FOR (n:Entidad) ON (n.k)",
                  "CREATE INDEX entidad_tipo IF NOT EXISTS FOR (n:Entidad) ON (n.tipo)",
                  "CREATE FULLTEXT INDEX nombres IF NOT EXISTS FOR (n:Entidad) ON EACH [n.nombre]"]:
            driver.execute_query(c)
        for tipo, g in N.drop_duplicates("id").groupby("tipo"):
            if not re.fullmatch(r"[A-Za-z]+", str(tipo)):
                continue
            filas = [{"id": f["id"], "props": {**propiedades(f, {"id"}), "id": f["id"]}} for f in g.astype(object).where(g.notna(), None).to_dict("records")]
            for i in range(0, len(filas), args.lote):
                driver.execute_query(f"UNWIND $filas AS f MERGE (n:Entidad {{id: f.id}}) SET n += f.props SET n:{tipo}", filas=filas[i:i + args.lote])
        print(f"Nodos: {N.id.nunique():,} · {time.time() - t0:.0f} s", flush=True)
        for tipo, g in R.groupby("tipo"):
            if not re.fullmatch(r"[A-Z_]+", str(tipo)):
                continue
            filas = [{"s": f["s"], "o": f["o"], "fid": f["fid"], "props": propiedades(f, {"s", "o", "tipo", "fid"})}
                     for f in g.astype(object).where(g.notna(), None).to_dict("records")]
            for i in range(0, len(filas), args.lote):
                driver.execute_query(f"UNWIND $filas AS f MATCH (a:Entidad {{id: f.s}}) MATCH (b:Entidad {{id: f.o}}) "
                                     f"MERGE (a)-[x:{tipo} {{fid: f.fid}}]->(b) SET x += f.props", filas=filas[i:i + args.lote])
        n = driver.execute_query("MATCH (n:Entidad) RETURN count(n) AS n").records[0]["n"]
        r = driver.execute_query("MATCH ()-[r]->() RETURN count(r) AS n").records[0]["n"]
    print(f"Listo: {n:,} nodos · {r:,} relaciones · {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
