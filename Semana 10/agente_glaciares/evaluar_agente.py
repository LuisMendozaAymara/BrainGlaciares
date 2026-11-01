# -*- coding: utf-8 -*-
"""Evalúa el agente con un banco de preguntas y deja los archivos para la evaluación con RAGAS.

    python evaluar_agente.py --url https://tu-servicio.example --salida resultados     # contra el servicio desplegado
    python evaluar_agente.py --salida resultados                                       # en este proceso (usa .env)

Para evaluar otro banco: --banco mi_banco.json  (lista de {"id", "tipo", "pregunta"}).
Salida: respuestas_agente.csv, ragas_agente.jsonl (mismo formato que ragas_semana10.jsonl) y reporte_agente.md.
"""
import argparse
import csv
import json
import os
import re
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
COLUMNAS = ["id", "tipo", "pregunta", "cumple", "responde", "modo", "modelo", "autocorregida", "herramientas", "n_citas", "citas_grafo",
            "citas_texto", "docs_citados", "instituciones_citadas", "avisos", "segundos", "respuesta"]


def cliente_url(url, clave=None):
    import urllib.error
    import urllib.request

    def preguntar(texto):
        cuerpo = json.dumps({"pregunta": texto}).encode("utf-8")
        cab = {"Content-Type": "application/json"}
        if clave:
            cab["X-API-Key"] = clave
        for intento in range(6):
            req = urllib.request.Request(url.rstrip("/") + "/api/preguntar", data=cuerpo, headers=cab, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503) or intento == 5:
                    raise
                time.sleep(int(e.headers.get("Retry-After") or 30))
    return preguntar


def cliente_local():
    from config import cargar_config
    from servicio import Servicio
    srv = Servicio(cargar_config())
    srv.cargar()
    if srv.estado != "listo":
        sys.exit(f"El agente no pudo iniciar: {srv.detalle}")
    return srv.responder


def cumple(b, r):
    """Con un banco que trae lo esperado ("debe", "no_debe", "visual", "modo"), dice si la respuesta lo cumple."""
    if not any(k in b for k in ("debe", "no_debe", "visual", "modo")):
        return ""
    ok = True
    if "debe" in b:
        ok &= bool(re.search(b["debe"], r["respuesta"]))
    if "no_debe" in b:
        ok &= not re.search(b["no_debe"], r["respuesta"])
    if "modo" in b:
        ok &= r["modo"] == b["modo"]
    if "visual" in b:
        ok &= any(v.get("tipo") == b["visual"] for v in r.get("visualizaciones", []))
    return "sí" if ok else "NO"


def fila(b, r):
    return {"id": b["id"], "tipo": b.get("tipo", ""), "pregunta": b["pregunta"], "cumple": cumple(b, r),
            "responde": r["modo"] in ("agente", "graphrag") and not r["respuesta"].startswith("No encontré"),
            "modo": r["modo"], "modelo": r.get("modelo"), "autocorregida": r.get("autocorregida", False),
            "herramientas": " > ".join(r.get("herramientas", [])), "n_citas": r["n_citas"], "citas_grafo": r["n_citas_grafo"],
            "citas_texto": r["n_citas_texto"], "docs_citados": len(r["docs_citados"]), "instituciones_citadas": ", ".join(r["instituciones"]),
            "avisos": " ".join(r["avisos"]), "segundos": r["segundos"], "respuesta": r["respuesta"]}


def ragas(b, r):
    grafo = [f for f in r["fuentes"] if f["tipo"] == "grafo"][:15]
    herr = r.get("herramientas", [])
    return {"id": b["id"], "question": b["pregunta"], "answer": r["respuesta"],
            "reference": "\n".join(f"- {f['texto']}" for f in grafo),
            "reference_source": ("grafo · " + " > ".join(herr)) if grafo else "",
            "contexts": [f["texto"] + (f" | evidencia: {f['evidencia']}" if f.get("evidencia") else "") for f in r["fuentes"]],
            "sources": [f["referencia"] for f in r["fuentes"]], "tool": " > ".join(herr), "model": r.get("modelo")}


def reporte(filas, origen):
    comp = [f for f in filas if f["tipo"] == "competencia"]
    lin = ["# Reporte del agente · semana 10", "", f"- Evaluado en: {origen}",
           f"- Preguntas de competencia respondidas: {sum(f['responde'] for f in comp)}/{len(comp)} · con citas: {sum(f['n_citas'] > 0 for f in comp)}/{len(comp)}",
           f"- Modos: " + ", ".join(f"{m}: {sum(f['modo'] == m for f in filas)}" for m in sorted({f['modo'] for f in filas})),
           f"- Tiempo promedio: {sum(f['segundos'] for f in filas) / max(len(filas), 1):.1f} s", "",
           "| id | tipo | herramientas | modo | modelo | responde | citas (grafo) | s | avisos |", "|---|---|---|---|---|---|---|---|---|"]
    for f in filas:
        avisos = re.sub(r"\s+", " ", f["avisos"]).replace("|", ";")
        lin.append(f"| {f['id']} | {f['tipo']} | {f['herramientas'] or '—'} | {f['modo']} | {f['modelo'] or '—'} | {'✅' if f['responde'] else '❌'} | "
                   f"{f['n_citas']} ({f['citas_grafo']}) | {f['segundos']} | {avisos} |")
    return "\n".join(lin) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="URL del servicio desplegado; sin ella se evalúa en este proceso")
    p.add_argument("--banco", default=os.path.join(AQUI, "banco_preguntas.json"))
    p.add_argument("--salida", default=os.path.join(AQUI, "resultados"))
    p.add_argument("--pausa", type=float, default=6.0, help="segundos entre preguntas (límite gratuito de los LLM)")
    a = p.parse_args()
    banco = json.load(open(a.banco, encoding="utf-8"))
    preguntar = cliente_url(a.url, os.environ.get("AGENTE_API_KEY")) if a.url else cliente_local()
    os.makedirs(a.salida, exist_ok=True)
    filas, jsonl = [], []
    for i, b in enumerate(banco):
        r = preguntar(b["pregunta"])
        filas.append(fila(b, r))
        jsonl.append(ragas(b, r))
        f = filas[-1]
        print(f"{b['id']:>4} · {('cumple: ' + f['cumple'] + ' · ') if f['cumple'] else ''}{f['herramientas'] or '—':<45} · {'ok' if f['responde'] else 'no'} · citas {f['n_citas']} · {f['segundos']} s · {f['modelo'] or f['modo']}", flush=True)
        if a.pausa and i < len(banco) - 1 and not r.get("desde_cache"):
            time.sleep(a.pausa)
    with open(os.path.join(a.salida, "respuestas_agente.csv"), "w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(filas)
    with open(os.path.join(a.salida, "ragas_agente.jsonl"), "w", encoding="utf-8") as h:
        h.writelines(json.dumps(x, ensure_ascii=False) + "\n" for x in jsonl)
    with open(os.path.join(a.salida, "reporte_agente.md"), "w", encoding="utf-8") as h:
        h.write(reporte(filas, a.url or "proceso local"))
    print(f"Resultados en {a.salida}")


if __name__ == "__main__":
    main()
