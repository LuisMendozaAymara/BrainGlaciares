# -*- coding: utf-8 -*-
"""
Semana 10 — GraphRAG · Enterprise Brain Glaciares (Grupo G8)

Combina las dos memorias del proyecto para responder preguntas de varios saltos citando la fuente:
  · el GRAFO de conocimiento en Neo4j (semana 7): hechos estructurados con documento, página y frase de evidencia;
  · el RAG de la semana 9 (bge-m3 + Chroma + BM25): fragmentos de texto de los 398 documentos.

Flujo de una pregunta (cada paso queda registrado en r["plan"]):
  1. Enlace de entidades: se reconocen en la pregunta los nombres del grafo (lagunas, glaciares, poblados…).
  2. Ruta: el LLM elige la herramienta (o, sin LLM, reglas de palabras clave):
       · consultas verificadas P1–P10 y E1–E10 (búsqueda "global": listas, conteos, cadenas de varios saltos);
       · ficha de una entidad (búsqueda "local", como el modo local de LightRAG);
       · camino entre dos entidades (shortestPath);
       · Text2Cypher: el LLM escribe una consulta de solo lectura que se valida antes de ejecutarla;
       · solo texto (preguntas explicativas).
  3. Texto guiado por el grafo: el buscador híbrido de la semana 9 trae fragmentos y sube los de los documentos
     que respaldan los hechos encontrados.
  4. Respuesta: el LLM recibe hechos [n] y fragmentos [n] numerados y debe citar cada dato.
  5. Verificación: citas inexistentes, oraciones con datos sin cita y cifras que no están en la evidencia.

Uso (ver semana10_graphrag_glaciares.ipynb):
    G = GrafoNeo4j(uri, usuario, clave)           # o GrafoLocal(carpeta_grafo) si Neo4j no está disponible
    motor = GraphRAG(G, indice=INDICE, llm=LLM, R=R)
    r = motor.responder("¿Qué glaciares alimentan lagunas en riesgo y qué poblados amenazan?")
    display(Markdown(a_markdown(r)))
"""
import json
import os
import re
import time
import unicodedata

import pandas as pd

VERSION = "1.0-graphrag"

CONFIG_GR = {
    "MAX_FILAS": 20,          # filas de una consulta al grafo que lee el LLM (el total siempre se informa)
    "MAX_HECHOS_FICHA": 30,   # hechos de la ficha de una entidad
    "K_TEXTO": 4,             # fragmentos de texto que acompañan a los hechos
    "MAX_POR_DOC": 2,         # máximo de fragmentos de un mismo documento
    "LIMIT_CYPHER": 50,       # límite que se impone a las consultas escritas por el LLM
    "PALABRAS": 250,          # extensión máxima de la respuesta
    "REINTENTOS_LLM": 5,
    "RUTA_LLM_SIEMPRE": False,  # False = si las reglas reconocen la pregunta, no se gasta una llamada en el enrutador
    "AUTOCORRECCION": True,     # si la verificación encuentra cifras o citas dudosas, el LLM corrige una vez su respuesta
}

NO_RELACION = ["mencionaA", "usaMetodo", "publicadoPor"]   # relaciones de documentos (no son hechos del dominio)
TIPOS_SIN_ENLACE = {"Documento", "Medicion", "NivelRiesgo", "EstadoObra"}


def sin_tildes(t):
    return "".join(c for c in unicodedata.normalize("NFD", t or "") if unicodedata.category(c) != "Mn")


def norm(t):
    return re.sub(r"\s+", " ", sin_tildes(str(t or "").lower())).strip()


# ======================================================================
# 1. Consultas verificadas del grafo (copiadas del módulo de la semana 7, con los mismos resultados 10/10)
# ======================================================================
import hashlib  # noqa: F401

DEPARTAMENTOS = ["Áncash", "Junín", "Cusco", "Puno", "Huánuco", "Apurímac", "Huancavelica", "Arequipa", "Ayacucho",
                 "Pasco", "Cajamarca", "Moquegua", "Tacna", "Lima", "La Libertad", "Piura", "Amazonas", "San Martín",
                 "Ica", "Lambayeque", "Tumbes", "Loreto", "Ucayali", "Madre de Dios"]

EN_RIESGO = ["Muy alto", "Alto"]
METODOS_BALANCE = ["Balance de masa", "Estacas de ablación"]

P_RIESGO = {"niveles": EN_RIESGO}
CONSULTAS = {
    "P1": dict(
        titulo="¿Cuántas lagunas de origen glaciar se formaron en la Cordillera Blanca según el inventario de 2023?",
        params={"cordillera": "blanca", "anio": 2023},
        cypher="""
MATCH (i:Inventario)-[:INVENTARIO_DE]->(c:Cordillera {k: $cordillera})
WHERE i.anio = $anio
MATCH (i)-[:TIENE_MEDICION]->(m:Medicion)
WHERE m.propiedad IN ['registraLagunas', 'registraLagunasNuevas', 'registraLagunasProglaciares', 'registraGlaciares', 'registraSuperficieGlaciar']
RETURN i.nombre AS inventario, m.propiedad AS dato, m.texto AS valor,
       m.documento AS documento, m.pagina AS pagina, m.evidencia AS evidencia
ORDER BY dato"""),
    "P2": dict(
        titulo="¿Qué lagunas glaciares de Áncash tienen riesgo muy alto de desborde?",
        params={"departamento": "Áncash", "nivel": "Muy alto"},
        cypher="""
MATCH (l:Laguna)-[r:TIENE_NIVEL_RIESGO]->(n:NivelRiesgo)
WHERE n.nombre = $nivel AND $departamento IN l.departamentos
RETURN l.nombre AS laguna, n.nombre AS nivel, r.n_docs AS n_docs,
       r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY n_docs DESC, laguna"""),
    "P3": dict(
        titulo="¿Qué centros poblados están aguas abajo de lagunas con riesgo alto y qué estudios los evaluaron?",
        params=P_RIESGO,
        cypher="""
MATCH (l:Laguna)-[r:AMENAZA_A]->(p:CentroPoblado)
WHERE l.nivel_max IN $niveles AND p.dudoso = false
OPTIONAL MATCH (d:Documento)-[:MENCIONA]->(l), (d)-[:MENCIONA]->(p)
RETURN l.nombre AS laguna, l.nivel_max AS nivel, p.nombre AS poblado, 'el texto lo dice' AS via, '' AS subcuenca,
       collect(DISTINCT d.id) AS estudios, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
UNION
MATCH (l:Laguna)-[:UBICADA_EN]->(c)<-[r:UBICADA_EN]-(p:CentroPoblado)
WHERE l.nivel_max IN $niveles AND c.hidro = true AND c.grande = false AND p.dudoso = false
OPTIONAL MATCH (d:Documento)-[:MENCIONA]->(l), (d)-[:MENCIONA]->(p)
RETURN l.nombre AS laguna, l.nivel_max AS nivel, p.nombre AS poblado, 'misma subcuenca' AS via, c.nombre AS subcuenca,
       collect(DISTINCT d.id) AS estudios, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia"""),
    "P4": dict(
        titulo="¿Qué lagunas tuvieron eventos de desborde, de qué glaciar provienen y qué poblados afectaron?",
        params={},
        cypher="""
MATCH (e:Evento)-[r:OCURRIO_EN]->(l:Laguna)
OPTIONAL MATCH (l)-[:ALIMENTADA_POR]->(g)
OPTIONAL MATCH (e)-[:AFECTO_A]->(p)
OPTIONAL MATCH (e)-[:TIENE_MEDICION]->(v:Medicion)
WHERE v.propiedad = 'causoVictimas'
RETURN e.nombre AS evento, e.anio AS anio, l.nombre AS laguna,
       collect(DISTINCT g.nombre) AS glaciar_origen, collect(DISTINCT p.nombre) AS poblados_afectados,
       collect(DISTINCT v.texto) AS victimas, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY anio"""),
    "P5": dict(
        titulo="¿Qué glaciares con retroceso medido (balance de masa) alimentan lagunas que hoy están en riesgo?",
        params={**P_RIESGO, "balance": METODOS_BALANCE},
        cypher="""
MATCH (l:Laguna)-[ra:ALIMENTADA_POR]->(g)
WHERE l.nivel_max IN $niveles AND g.tipo IN ['Glaciar', 'Nevado']
OPTIONAL MATCH (g)-[:MONITOREADO_CON]->(m:MetodoMonitoreo)
WHERE m.nombre IN $balance
OPTIONAL MATCH (g)-[:PRESENTA_FENOMENO]->(f:Fenomeno)
WHERE f.nombre = 'Retroceso glaciar'
OPTIONAL MATCH (d:Documento)-[:MENCIONA]->(g), (d)-[:USA_METODO]->(mb:MetodoMonitoreo)
WHERE mb.nombre IN $balance
WITH l, g, ra, count(DISTINCT m) AS n_medido, count(DISTINCT f) AS n_reportado, count(DISTINCT d) AS n_doc
WHERE n_medido + n_reportado + n_doc > 0
RETURN g.nombre AS glaciar,
       CASE WHEN n_medido > 0 THEN 'medido (balance de masa)'
            WHEN n_reportado > 0 THEN 'retroceso reportado'
            ELSE 'en documento con balance de masa' END AS retroceso,
       l.nombre AS laguna, l.nivel_max AS nivel, ra.documento AS documento, ra.pagina AS pagina, ra.evidencia AS evidencia
ORDER BY retroceso DESC, glaciar"""),
    "P6": dict(
        titulo="¿Qué métodos de monitoreo se aplicaron a lagunas en riesgo y qué institución realizó cada estudio?",
        params=P_RIESGO,
        cypher="""
MATCH (l:Laguna)-[r:MONITOREADO_CON]->(m:MetodoMonitoreo)
WHERE l.nivel_max IN $niveles
OPTIONAL MATCH (i:Institucion)-[:INTERVIENE_EN]->(l)
OPTIONAL MATCH (doc:Documento)-[:PUBLICADO_POR]->(pub:Institucion)
WHERE doc.id = r.documento
RETURN l.nombre AS laguna, m.nombre AS metodo, collect(DISTINCT i.nombre) AS instituciones_en_texto,
       collect(DISTINCT pub.nombre) AS institucion_del_documento,
       r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY laguna, metodo"""),
    "P7": dict(
        titulo="¿En qué subcuencas coinciden lagunas en riesgo y contaminación por drenaje ácido de roca?",
        params={**P_RIESGO, "fenomeno": "Drenaje ácido de roca"},
        cypher="""
MATCH (l:Laguna)-[:UBICADA_EN]->(c)
WHERE l.nivel_max IN $niveles AND c.hidro = true AND c.grande = false
MATCH (f:Fenomeno)
WHERE f.nombre = $fenomeno
OPTIONAL MATCH (c)-[r1:PRESENTA_FENOMENO]->(f)
OPTIONAL MATCH (x)-[:UBICADA_EN]->(c), (x)-[r2:PRESENTA_FENOMENO]->(f)
WITH c, l, r1, x, r2
WHERE r1 IS NOT NULL OR r2 IS NOT NULL
RETURN c.nombre AS subcuenca, collect(DISTINCT l.nombre) AS lagunas_en_riesgo,
       collect(DISTINCT x.nombre) AS con_drenaje_acido,
       collect(DISTINCT coalesce(r1.documento, r2.documento)) AS documentos,
       collect(DISTINCT coalesce(r1.evidencia, r2.evidencia)) AS evidencias
ORDER BY subcuenca"""),
    "P8": dict(
        titulo="¿Qué bofedales están en subcuencas con glaciares en retroceso y qué servicios ecosistémicos brindan?",
        params={},
        cypher="""
MATCH (g)-[:PRESENTA_FENOMENO]->(f:Fenomeno)
WHERE f.nombre = 'Retroceso glaciar' AND g.tipo IN ['Glaciar', 'Nevado', 'Cuenca', 'Quebrada']
MATCH (b:Bofedal)-[rb:UBICADA_EN]->(c)
WHERE c.hidro = true AND c.grande = false AND b.dudoso = false AND (c.id = g.id OR EXISTS { MATCH (g)-[:UBICADA_EN]->(c) })
OPTIONAL MATCH (b)-[:PROVEE_SERVICIO]->(s:ServicioEcosistemico)
OPTIONAL MATCH (e:Ecosistema)-[:PROVEE_SERVICIO]->(s2:ServicioEcosistemico)
WHERE e.k = 'bofedal'
RETURN b.nombre AS bofedal, c.nombre AS subcuenca, collect(DISTINCT g.nombre) AS en_retroceso,
       collect(DISTINCT s.nombre) AS servicios_del_bofedal, collect(DISTINCT s2.nombre) AS servicios_de_los_bofedales,
       rb.documento AS documento, rb.pagina AS pagina, rb.evidencia AS evidencia
ORDER BY subcuenca, bofedal"""),
    "P9": dict(
        titulo="¿Cómo cambió la superficie glaciar de una cordillera entre los inventarios de 2018 y 2023?",
        params={"cordillera": "blanca"},
        cypher="""
MATCH (i:Inventario)-[:INVENTARIO_DE]->(c:Cordillera {k: $cordillera})
MATCH (i)-[:TIENE_MEDICION]->(m:Medicion)
WHERE m.propiedad = 'registraSuperficieGlaciar' AND m.valor IS NOT NULL
RETURN i.anio AS anio, i.anio_imagenes AS anio_imagenes, m.valor AS km2, m.confianza AS confianza,
       m.documento AS documento, m.pagina AS pagina, m.evidencia AS evidencia
ORDER BY anio"""),
    "P10": dict(
        titulo="¿Qué lagunas en riesgo cuentan con obras de seguridad, en qué estado están y qué poblados protegen?",
        params=P_RIESGO,
        cypher="""
MATCH (l:Laguna)-[r:TIENE_OBRA]->(o:ObraSeguridad)
WHERE l.nivel_max IN $niveles
OPTIONAL MATCH (l)-[re:ESTADO_OBRA]->(e:EstadoObra)
WHERE re.obra = o.nombre
OPTIONAL MATCH (l)-[:AMENAZA_A]->(p:CentroPoblado)
WHERE p.dudoso = false
RETURN l.nombre AS laguna, l.nivel_max AS nivel, o.nombre AS obra, collect(DISTINCT e.nombre) AS estado,
       collect(DISTINCT p.nombre) AS poblados_protegidos, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY laguna, obra"""),
}
EXTRAS = {
    "E1_negacion_sin_obras": dict(
        titulo="Negación · ¿Qué lagunas en riesgo (alto o muy alto) NO tienen obras de seguridad registradas?",
        params=P_RIESGO, cypher="""
MATCH (l:Laguna)-[r:TIENE_NIVEL_RIESGO]->(n:NivelRiesgo)
WHERE n.nombre IN $niveles AND NOT EXISTS { MATCH (l)-[:TIENE_OBRA]->(:ObraSeguridad) }
RETURN l.nombre AS laguna, n.nombre AS nivel, l.departamentos AS departamentos, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY nivel DESC, laguna"""),
    "E2_negacion_sin_monitoreo": dict(
        titulo="Negación · ¿Qué lagunas en riesgo no tienen ningún método de monitoreo registrado?",
        params=P_RIESGO, cypher="""
MATCH (l:Laguna)
WHERE l.nivel_max IN $niveles AND NOT EXISTS { MATCH (l)-[:MONITOREADO_CON]->(:MetodoMonitoreo) }
RETURN l.nombre AS laguna, l.nivel_max AS nivel, l.departamentos AS departamentos
ORDER BY laguna"""),
    "E3_conteo_departamento": dict(
        titulo="Conteo · ¿Cuántas lagunas en riesgo hay por departamento?",
        params=P_RIESGO, cypher="""
MATCH (l:Laguna)
WHERE l.nivel_max IN $niveles
UNWIND l.departamentos AS departamento
RETURN departamento, count(DISTINCT l) AS lagunas_en_riesgo, collect(DISTINCT l.nombre) AS lagunas
ORDER BY lagunas_en_riesgo DESC"""),
    "E4_conteo_cordillera": dict(
        titulo="Conteo con varios saltos · ¿Qué cordillera tiene más lagunas en riesgo?",
        params=P_RIESGO, cypher="""
MATCH (l:Laguna)-[:UBICADA_EN*1..3]->(c:Cordillera)
WHERE l.nivel_max IN $niveles
RETURN c.nombre AS cordillera, count(DISTINCT l) AS lagunas_en_riesgo, collect(DISTINCT l.nombre) AS lagunas
ORDER BY lagunas_en_riesgo DESC"""),
    "E5_ranking_volumen": dict(
        titulo="Ranking · Las 5 lagunas de Áncash con mayor volumen",
        params={"departamento": "Áncash"}, cypher="""
MATCH (l:Laguna)-[:TIENE_MEDICION]->(m:Medicion)
WHERE m.propiedad = 'tieneVolumen' AND m.valor IS NOT NULL AND $departamento IN l.departamentos
RETURN l.nombre AS laguna, max(m.valor) AS volumen_m3, collect(DISTINCT m.documento) AS documentos
ORDER BY volumen_m3 DESC LIMIT 5"""),
    "E6_linea_tiempo": dict(
        titulo="Línea de tiempo · Eventos registrados antes de 1980",
        params={"hasta": 1980}, cypher="""
MATCH (e:Evento)
WHERE e.anio < $hasta
OPTIONAL MATCH (e)-[r:OCURRIO_EN]->(x)
OPTIONAL MATCH (e)-[:AFECTO_A]->(p)
RETURN e.anio AS anio, e.nombre AS evento, collect(DISTINCT x.nombre) AS lugar, collect(DISTINCT p.nombre) AS afecto_a,
       collect(DISTINCT r.documento) AS documentos
ORDER BY anio"""),
    "E7_instituciones": dict(
        titulo="Conteo · Instituciones que intervienen en más lagunas",
        params={}, cypher="""
MATCH (i:Institucion)-[:INTERVIENE_EN]->(l:Laguna)
RETURN i.nombre AS institucion, count(DISTINCT l) AS lagunas, collect(DISTINCT l.nombre) AS ejemplos
ORDER BY lagunas DESC LIMIT 10"""),
    "E8_ficha": dict(
        titulo="Ficha · Todo lo que se sabe de la laguna Palcacocha (con fuente)",
        params={"k": "palcacocha"}, cypher="""
MATCH (a:Entidad {k: $k})-[r]-(b)
WHERE r.pred <> 'mencionaA'
RETURN a.tipo AS tipo, r.pred AS relacion, b.nombre AS con, b.tipo AS tipo_con, r.n_docs AS n_docs,
       r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY n_docs DESC LIMIT 40"""),
    "E9_comencion": dict(
        titulo="Co-mención · Documentos que hablan a la vez de Palcacocha y Huaraz",
        params={"a": "palcacocha", "b": "huaraz"}, cypher="""
MATCH (d:Documento)-[:MENCIONA]->(a:Entidad {k: $a}), (d)-[:MENCIONA]->(b:Entidad {k: $b})
RETURN DISTINCT d.id AS documento, d.titulo AS titulo, d.fuente AS fuente, d.anio AS anio
ORDER BY anio DESC"""),
    "E10_camino": dict(
        titulo="Camino · ¿Cómo se conecta el nevado Huascarán con Yungay? (sin pasar por documentos)",
        params={"a": "huascaran", "b": "yungay"}, solo_neo4j=True, cypher="""
MATCH (a:Entidad {k: $a}), (b:Entidad {k: $b})
MATCH p = shortestPath((a)-[*..5]-(b))
WHERE ALL(r IN relationships(p) WHERE NOT type(r) IN ['MENCIONA', 'USA_METODO', 'PUBLICADO_POR'])
RETURN [n IN nodes(p) | n.nombre] AS camino, [r IN relationships(p) | r.pred] AS relaciones,
       [r IN relationships(p) | r.documento] AS documentos
LIMIT 3"""),
}


def comparar_superficie(df, a1=2018, a2=2023):
    """P9: elige el valor de cada año (verificado primero; sin atípicos) y calcula el cambio."""
    if df is None or not len(df):
        return pd.DataFrame()
    d = df.dropna(subset=["anio", "km2"]).copy()
    d["anio"] = d.anio.astype(int)
    med = d.km2.median()
    d = d[(d.km2 >= 0.4 * med) & (d.km2 <= 2.5 * med)].sort_values(["anio", "confianza"], ascending=[True, False])
    serie = d.drop_duplicates("anio")
    if not len(serie):
        return pd.DataFrame()
    cerca = lambda a: serie.iloc[(serie.anio - a).abs().argsort().iloc[0]]
    x, y = cerca(a1), cerca(a2)
    if x.anio == y.anio and len(serie) > 1:
        antes = serie[serie.anio < y.anio]
        x = antes.iloc[-1] if len(antes) else serie.iloc[0]
    nota = "" if (x.anio, y.anio) == (a1, a2) else f"años pedidos {a1}/{a2}; se usan los más cercanos con dato"
    if y.km2 > x.km2:
        nota += " ⚠️ aumento: revisar fuentes"
    return pd.DataFrame([{"anio_1": int(x.anio), "km2_1": x.km2, "anio_2": int(y.anio), "km2_2": y.km2,
                          "cambio_km2": round(y.km2 - x.km2, 2), "cambio_%": round(100 * (y.km2 - x.km2) / x.km2, 1),
                          "fuente_1": f"{x.documento} p.{x.pagina}", "fuente_2": f"{y.documento} p.{y.pagina}", "nota": nota.strip()}])


ROTULOS = {   # descripción corta de cada consulta para el enrutador
    "P1": "conteo de lagunas/glaciares de una cordillera según un inventario (parámetros: cordillera, anio)",
    "P2": "lagunas de un departamento con un nivel de riesgo (parámetros: departamento, nivel)",
    "P3": "centros poblados aguas abajo de lagunas en riesgo y estudios que los evaluaron (parámetro: niveles)",
    "P4": "eventos de desborde/aluviones en lagunas, glaciar de origen y poblados afectados",
    "P5": "glaciares con retroceso medido (balance de masa) que alimentan lagunas en riesgo",
    "P6": "métodos de monitoreo aplicados a lagunas en riesgo e institución de cada estudio",
    "P7": "subcuencas donde coinciden lagunas en riesgo y drenaje ácido de roca",
    "P8": "bofedales en subcuencas con glaciares en retroceso y servicios ecosistémicos",
    "P9": "cambio de la superficie glaciar de una cordillera entre inventarios (parámetro: cordillera)",
    "P10": "lagunas en riesgo con obras de seguridad, estado de las obras y poblados protegidos",
    "E1_negacion_sin_obras": "lagunas en riesgo que NO tienen obras de seguridad",
    "E2_negacion_sin_monitoreo": "lagunas en riesgo sin ningún método de monitoreo",
    "E3_conteo_departamento": "cuántas lagunas en riesgo hay por departamento (conteo)",
    "E4_conteo_cordillera": "qué cordillera tiene más lagunas en riesgo (conteo)",
    "E5_ranking_volumen": "ranking de lagunas de un departamento por volumen (parámetro: departamento)",
    "E6_linea_tiempo": "eventos registrados antes de un año (parámetro: hasta)",
    "E7_instituciones": "instituciones que intervienen en más lagunas (ranking general, sin filtros)",
    "E11_poblados_y_glaciares": "poblados aguas abajo de lagunas en riesgo Y glaciares que alimentan esas lagunas (varios saltos)",
}
EXTRAS["E11_poblados_y_glaciares"] = dict(
    titulo="Varios saltos · Poblados aguas abajo de lagunas en riesgo y glaciares que alimentan esas lagunas",
    params={"niveles": EN_RIESGO}, cypher="""
MATCH (l:Laguna)-[r:AMENAZA_A]->(p:CentroPoblado)
WHERE l.nivel_max IN $niveles AND p.dudoso = false
OPTIONAL MATCH (l)-[:ALIMENTADA_POR]->(g)
WHERE g.tipo IN ['Glaciar', 'Nevado']
RETURN l.nombre AS laguna, l.nivel_max AS nivel, p.nombre AS poblado, collect(DISTINCT g.nombre) AS glaciares_que_la_alimentan,
       r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY laguna, poblado""")
PLANTILLAS = {**CONSULTAS, **{k: v for k, v in EXTRAS.items() if k in ROTULOS}}

# reglas de respaldo (sin LLM): palabras clave → consulta
REGLAS_RUTA = [
    ("E3_conteo_departamento", [r"cu[aá]nt", r"departamento"]),
    ("E4_conteo_cordillera", [r"cordillera", r"m[aá]s lagunas"]),
    ("P7", [r"drenaje [aá]cido"]),
    ("P8", [r"bofedal"]),
    ("P5", [r"balance de masa", r"retroceso medido"]),
    ("P9", [r"superficie glaciar", r"(2018|2016).*(2023)|cambi[oó] la superficie"]),
    ("P1", [r"inventario", r"cu[aá]ntas lagunas|se formaron|lagunas nuevas"]),
    ("P10", [r"obras? de seguridad|diques?|t[uú]nel|desag[uü]e", r"estado|proteg"]),
    ("E1_negacion_sin_obras", [r"\bno (tienen|cuentan)|\bsin obras?"]),
    ("E2_negacion_sin_monitoreo", [r"sin (ning[uú]n )?(m[eé]todo de )?monitoreo|no (tienen|cuentan con) monitoreo"]),
    ("P2", [r"riesgo", r"\b(" + "|".join(sorted({norm(d) for d in DEPARTAMENTOS + ["Ancash"]})) + r")\b"]),
    ("P6", [r"monitoreo|m[eé]todos?"]),
    ("P4", [r"eventos?|aluvi[oó]n(es)?|se desbord", r"afect|provien|origen"]),
    ("E11_poblados_y_glaciares", [r"aguas abajo|centros? poblados?", r"glaciar(es)? (que )?alimenta"]),
    ("P3", [r"aguas abajo|centros? poblados?|poblados (expuestos|amenazados)"]),
    ("E5_ranking_volumen", [r"mayor volumen|m[aá]s grandes?|ranking"]),
    ("E7_instituciones", [r"instituci", r"m[aá]s lagunas|intervienen"]),
    ("E6_linea_tiempo", [r"antes de (19|20)\d\d"]),
]
PALABRAS_TEXTO = r"^\s*¿?\s*(por qu[eé]|c[oó]mo (funciona|se forma|afecta|influye)|qu[eé] (factores|causas|explica|significa|es (un|una|el|la)))"

GENERICAS = {norm(x) for x in """laguna lagunas glaciar glaciares nevado nevados cordillera cordilleras rio rios cuenca subcuenca
    microcuenca quebrada bofedal bofedales riesgo alto muy alto medio bajo desborde aluvion evento eventos poblado poblados
    centro centros departamento provincia distrito peru agua aguas abajo monitoreo metodo metodos obra obras dique estado
    retroceso balance masa superficie inventario lagunas glaciares institucion instituciones estudio estudios documento
    servicio servicios ecosistema ecosistemas especie especies fenomeno fenomenos santa rosa""".split()}
NO_ENTIDAD = {norm(x) for x in """como cuando cada donde cual cuales quien quienes cuanto cuanta cuantos cuantas entre sobre desde hasta
    segun tiene tienen hay esta estan este estos fue fueron son ser han para por con sin del las los una uno unos unas mas menos
    muy tambien ademas todo toda todos todas otro otra otros otras mismo misma region instituto informe estudio estudios zona
    parte nivel norte sur este oeste alta baja grande chico nueva vieja centros poblados centro poblado gobierno regional
    nacional peru andes sierra costa selva agua hielo nieve que""".split()}
PISTAS_TIPO = {"laguna": "Laguna", "lagunas": "Laguna", "glaciar": "Glaciar", "nevado": "Nevado", "cordillera": "Cordillera",
               "rio": "Rio", "subcuenca": "Cuenca", "cuenca": "Cuenca", "microcuenca": "Cuenca", "quebrada": "Quebrada",
               "bofedal": "Bofedal", "distrito": "Distrito", "provincia": "Provincia", "departamento": "Departamento",
               "ciudad": "CentroPoblado", "poblado": "CentroPoblado", "localidad": "CentroPoblado"}
DEPARTAMENTOS_N = {norm(d): d for d in DEPARTAMENTOS + ["Ancash"]}


# ======================================================================
# 2. Acceso al grafo: Neo4j (principal) o copia local en CSV (respaldo)
# ======================================================================
class GrafoNeo4j:
    """Conexión de solo lectura a Neo4j (AuraDB). Todas las consultas devuelven DataFrames."""
    cypher_ok = True

    def __init__(self, uri, usuario, clave_, base=None):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(usuario, clave_))
        self.driver.verify_connectivity()
        self.base = base
        self.nombre = "Neo4j"

    def q(self, cypher, **params):
        extra = {"database_": self.base} if self.base else {}
        try:
            from neo4j import RoutingControl
            extra["routing_"] = RoutingControl.READ
        except Exception:
            pass
        recs, _, keys = self.driver.execute_query(cypher, params, **extra)
        return pd.DataFrame([r.data() for r in recs], columns=keys)

    def cerrar(self):
        self.driver.close()

    # --- operaciones que usa el motor (también las tiene GrafoLocal) ---
    def catalogo(self):
        return self.q("""MATCH (n:Entidad) WHERE NOT n.tipo IN ['Documento', 'Medicion']
RETURN n.id AS id, n.nombre AS nombre, n.k AS k, n.tipo AS tipo, coalesce(n.n_docs, 0) AS n_docs,
       coalesce(n.dudoso, false) AS dudoso, n.departamentos AS departamentos, n.nivel_max AS nivel_max""")

    def documentos(self, ids):
        if not ids:
            return pd.DataFrame(columns=["id", "titulo", "fuente", "anio"])
        return self.q("MATCH (d:Entidad) WHERE d.id IN $ids RETURN d.id AS id, d.titulo AS titulo, d.fuente AS fuente, d.anio AS anio",
                      ids=list(ids))

    def ficha(self, ids, limite=30):
        cols = ("r.pred AS relacion, b.nombre AS con, b.tipo AS tipo_con, r.n_docs AS n_docs, r.confianza AS confianza, "
                "r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia")
        sale = self.q(f"""MATCH (a:Entidad)-[r]->(b:Entidad) WHERE a.id IN $ids AND NOT r.pred IN $no
RETURN a.nombre AS entidad, 'sale' AS sentido, {cols} ORDER BY r.n_docs DESC LIMIT $lim""", ids=list(ids), no=NO_RELACION, lim=limite)
        entra = self.q(f"""MATCH (b:Entidad)-[r]->(a:Entidad) WHERE a.id IN $ids AND NOT r.pred IN $no
RETURN a.nombre AS entidad, 'entra' AS sentido, {cols} ORDER BY r.n_docs DESC LIMIT $lim""", ids=list(ids), no=NO_RELACION, lim=limite)
        return pd.concat([sale, entra], ignore_index=True)

    def camino(self, a, b, max_saltos=5):
        d = self.q(f"""MATCH (a:Entidad {{id: $a}}), (b:Entidad {{id: $b}})
MATCH p = shortestPath((a)-[*..{int(max_saltos)}]-(b))
WHERE ALL(r IN relationships(p) WHERE NOT r.pred IN $no)
RETURN [n IN nodes(p) | n.nombre] AS nodos, [r IN relationships(p) | r.pred] AS relaciones,
       [r IN relationships(p) | r.documento] AS documentos, [r IN relationships(p) | r.pagina] AS paginas,
       [r IN relationships(p) | r.evidencia] AS evidencias
LIMIT 1""", a=a, b=b, no=NO_RELACION)
        filas = []
        for p in d.itertuples():
            for i, rel in enumerate(p.relaciones):
                filas.append({"paso": i + 1, "desde": p.nodos[i], "relacion": rel, "hacia": p.nodos[i + 1],
                              "documento": p.documentos[i], "pagina": p.paginas[i], "evidencia": p.evidencias[i]})
        return pd.DataFrame(filas)

    def aristas_entre(self, ks, limite=250):
        return self.q("""MATCH (a:Entidad)-[r]->(b:Entidad) WHERE a.k IN $ks AND b.k IN $ks AND NOT r.pred IN $no
RETURN a.nombre AS desde, a.tipo AS tipo_desde, r.pred AS relacion, b.nombre AS hacia, b.tipo AS tipo_hacia,
       r.documento AS documento, r.pagina AS pagina LIMIT $lim""", ks=list(ks), no=NO_RELACION, lim=limite)

    def esquema_texto(self):
        n = self.q("MATCH (n:Entidad) RETURN n.tipo AS tipo, count(*) AS n ORDER BY n DESC")
        r = self.q("MATCH (a:Entidad)-[r]->(b:Entidad) RETURN type(r) AS rel, r.pred AS pred, a.tipo AS desde, b.tipo AS hacia, "
                   "count(*) AS n ORDER BY n DESC LIMIT 60")
        return esquema_desde(n, r)


class GrafoLocal:
    """Respaldo sin Neo4j: lee nodos.csv y relaciones.csv (exportados en la semana 7). Solo fichas y caminos."""
    cypher_ok = False

    def __init__(self, carpeta):
        self.N = pd.read_csv(os.path.join(carpeta, "nodos.csv"), dtype=str, low_memory=False)
        self.R = pd.read_csv(os.path.join(carpeta, "relaciones.csv"), dtype=str, low_memory=False)
        self.N = self.N.rename(columns={c: c.split(":")[0] for c in self.N.columns})
        self.R = self.R.rename(columns={c: c.split(":")[0] for c in self.R.columns})
        if "id" not in self.N and ":ID" in self.N:
            self.N["id"] = self.N[":ID"]
        for c in ("s", "o"):
            if c not in self.R:
                self.R[c] = self.R[":START_ID" if c == "s" else ":END_ID"]
        self.R_todo = self.R
        self.R = self.R[~self.R.pred.isin(NO_RELACION)]
        self.nombre = "copia local (CSV)"
        self._n = self.N.set_index("id")

    def catalogo(self):
        d = self.N[~self.N.tipo.isin(TIPOS_SIN_ENLACE)].copy()
        d["k"] = d.get("k", d.nombre.map(norm)).fillna(d.nombre.map(norm))
        d["n_docs"] = pd.to_numeric(d.get("n_docs"), errors="coerce").fillna(0)
        d["dudoso"] = d.get("dudoso", "False").astype(str).str.lower().eq("true")
        return d[["id", "nombre", "k", "tipo", "n_docs", "dudoso"]]

    def documentos(self, ids):
        d = self.N[self.N.id.isin(list(ids))]
        return pd.DataFrame({"id": d.id, "titulo": d.get("titulo", d.nombre), "fuente": d.get("fuente"), "anio": d.get("anio")})

    def ficha(self, ids, limite=30):
        nom = self._n.nombre.to_dict()
        tipo = self._n.tipo.to_dict()
        filas = []
        for col, otro, sentido in (("s", "o", "sale"), ("o", "s", "entra")):
            for r in self.R[self.R[col].isin(list(ids))].itertuples():
                filas.append({"entidad": nom.get(getattr(r, col)), "sentido": sentido, "relacion": r.pred,
                              "con": nom.get(getattr(r, otro)), "tipo_con": tipo.get(getattr(r, otro)),
                              "n_docs": pd.to_numeric(getattr(r, "n_docs", None), errors="coerce"),
                              "confianza": pd.to_numeric(getattr(r, "confianza", None), errors="coerce"),
                              "documento": getattr(r, "documento", None), "pagina": getattr(r, "pagina", None),
                              "evidencia": getattr(r, "evidencia", None)})
        d = pd.DataFrame(filas)
        return d.sort_values("n_docs", ascending=False).head(2 * limite) if len(d) else d

    def camino(self, a, b, max_saltos=5):
        import networkx as nx
        G = nx.Graph()
        for r in self.R.itertuples():
            G.add_edge(r.s, r.o, pred=r.pred, documento=getattr(r, "documento", None),
                       pagina=getattr(r, "pagina", None), evidencia=getattr(r, "evidencia", None))
        try:
            p = nx.shortest_path(G, a, b)
        except Exception:
            return pd.DataFrame()
        if len(p) - 1 > max_saltos:
            return pd.DataFrame()
        nom = self._n.nombre.to_dict()
        return pd.DataFrame([{"paso": i + 1, "desde": nom.get(x), "relacion": G[x][y]["pred"], "hacia": nom.get(y),
                              "documento": G[x][y]["documento"], "pagina": G[x][y]["pagina"], "evidencia": G[x][y]["evidencia"]}
                             for i, (x, y) in enumerate(zip(p, p[1:]))])

    def aristas_entre(self, ks, limite=250):
        k = self._n.get("k", self._n.nombre.map(norm))
        ids = set(k[k.isin(list(ks))].index)
        d = self.R[self.R.s.isin(ids) & self.R.o.isin(ids)].head(limite)
        nom, tipo = self._n.nombre.to_dict(), self._n.tipo.to_dict()
        return pd.DataFrame({"desde": d.s.map(nom), "tipo_desde": d.s.map(tipo), "relacion": d.pred, "hacia": d.o.map(nom),
                             "tipo_hacia": d.o.map(tipo), "documento": d.get("documento"), "pagina": d.get("pagina")})

    def esquema_texto(self):
        return "(sin Neo4j: no se pueden escribir consultas Cypher)"


class GrafoKuzu(GrafoLocal):
    cypher_libre_ok = False   # Text2Cypher desactivado: una consulta mal escrita podría cerrar Kùzu (y la sesión)
    """Respaldo con Cypher sin Neo4j: carga nodos.csv y relaciones.csv en Kùzu (base de grafos embebida, se arma en ~1 min).
    Ejecuta las consultas verificadas P1–P10 y E1–E7; las fichas, caminos y vistas se resuelven con pandas."""
    cypher_ok = True
    NCOLS = {"id": "STRING", "tipo": "STRING", "nombre": "STRING", "k": "STRING", "n_docs": "INT64", "anio": "INT64",
             "anio_imagenes": "INT64", "anio_formacion": "INT64", "departamentos": "STRING[]", "nivel_max": "STRING",
             "en_riesgo": "BOOL", "hidro": "BOOL", "n_poblados": "INT64", "grande": "BOOL", "dudoso": "BOOL", "titulo": "STRING",
             "fuente": "STRING", "eje": "STRING", "orden": "INT64", "propiedad": "STRING", "texto": "STRING", "valor": "DOUBLE",
             "unidad": "STRING", "documento": "STRING", "pagina": "INT64", "evidencia": "STRING", "confianza": "DOUBLE"}
    RCOLS = {"fid": "STRING", "pred": "STRING", "documento": "STRING", "pagina": "INT64", "evidencia": "STRING",
             "confianza": "DOUBLE", "n_docs": "INT64", "metodos": "STRING", "documentos": "STRING", "anio": "INT64",
             "verbo": "STRING", "obra": "STRING", "n": "INT64"}

    def __init__(self, carpeta, ruta_db=None, log=print):
        import shutil
        import tempfile
        ruta_db = ruta_db or ("/content/kuzu_glaciares" if os.path.isdir("/content") else os.path.join(tempfile.gettempdir(), "kuzu_glaciares"))
        import kuzu
        import pyarrow as pa
        super().__init__(carpeta)
        t0 = time.time()
        for p in (ruta_db, ruta_db + ".wal"):
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        self.c = kuzu.Connection(kuzu.Database(ruta_db))

        def columna(serie, tipo):
            if tipo == "STRING":
                return pa.array([None if _vacio(v) else str(v) for v in serie], pa.string())
            if tipo == "INT64":
                return pa.array([None if pd.isna(v) else int(v) for v in pd.to_numeric(serie, errors="coerce")], pa.int64())
            if tipo == "DOUBLE":
                return pa.array([None if pd.isna(v) else float(v) for v in pd.to_numeric(serie, errors="coerce")], pa.float64())
            if tipo == "BOOL":
                return pa.array([None if _vacio(v) else str(v).strip().lower() == "true" for v in serie], pa.bool_())
            return pa.array([[] if _vacio(v) else [x for x in str(v).split(";") if x] for v in serie], pa.list_(pa.string()))

        N = self.N.drop_duplicates("id")
        tipos = {}
        for t, g in N.groupby("tipo"):
            if not re.fullmatch(r"[A-Za-z]+", str(t)):
                continue
            cols = ", ".join(f"{k} {v}" for k, v in self.NCOLS.items())
            self.c.execute(f"CREATE NODE TABLE {t}({cols}, PRIMARY KEY(id))")
            tabla = pa.table({k: columna(g[k] if k in g else pd.Series([None] * len(g)), v) for k, v in self.NCOLS.items()})
            self.c.execute(f"COPY {t} FROM tabla")
            tipos.update(dict.fromkeys(g.id, t))
        R = self.R_todo.assign(ts=self.R_todo.s.map(tipos), to=self.R_todo.o.map(tipos)).dropna(subset=["ts", "to"])
        R = R.drop_duplicates(["s", "o", "tipo", "fid"])
        for rt, g in R.groupby("tipo"):
            if not re.fullmatch(r"[A-Z_]+", str(rt)):
                continue
            pares = g[["ts", "to"]].drop_duplicates().itertuples(index=False)
            pares = list(pares)
            cols = ", ".join(f"{k} {v}" for k, v in self.RCOLS.items())
            self.c.execute(f"CREATE REL TABLE {rt}({', '.join(f'FROM {a} TO {b}' for a, b in pares)}, {cols})")
            for a, b in pares:
                h = g[(g.ts == a) & (g.to == b)]
                datos = {"desde": pa.array(h.s.astype(str).tolist()), "hacia": pa.array(h.o.astype(str).tolist())}
                datos.update({k: columna(h[k] if k in h else pd.Series([None] * len(h)), v) for k, v in self.RCOLS.items()})
                tabla = pa.table(datos)
                self.c.execute(f"COPY {rt} FROM tabla (from='{a}', to='{b}')")
        self.nombre = "Kùzu local (copia del grafo)"
        log(f"Grafo local en Kùzu: {len(N):,} nodos · {len(R):,} relaciones · {time.time() - t0:.0f} s")

    def q(self, cypher, **params):
        cy = cypher.replace(":Entidad", "")
        cy = re.sub(r"\btype\((\w+)\)", r"label(\1)", cy)
        if "lim" in params:
            cy = cy.replace("LIMIT $lim", f"LIMIT {int(params.pop('lim'))}")
        return self.c.execute(cy, params).get_as_df()

    def esquema_texto(self):
        n = self.N.tipo.value_counts().rename_axis("tipo").reset_index(name="n")
        tip = dict(zip(self.N.id, self.N.tipo))
        r = (self.R_todo.assign(desde=self.R_todo.s.map(tip), hacia=self.R_todo.o.map(tip)).rename(columns={"tipo": "rel"})
             .groupby(["rel", "pred", "desde", "hacia"]).size().rename("n").reset_index().sort_values("n", ascending=False).head(60))
        return esquema_desde(n, r)


def esquema_desde(n, r):
    lin = ["NODOS: todos tienen la etiqueta :Entidad y además la de su tipo (por ejemplo :Entidad:Laguna).",
           "Propiedades de todos los nodos: id, nombre, k (nombre en minúsculas y sin tildes: úsalo para filtrar), tipo, n_docs.",
           "Lagunas y lugares: departamentos (lista, ej. ['Áncash']), nivel_max ('Muy alto'|'Alto'|'Medio'|'Bajo'), "
           "en_riesgo (bool), hidro (bool, subcuencas/quebradas), grande (bool, cuencas grandes), dudoso (bool, ignorar si true).",
           "Evento: anio. Inventario: anio, anio_imagenes. Medicion: propiedad (tieneVolumen, tieneArea, tieneAltitud, "
           "registraLagunas, registraSuperficieGlaciar…), valor (número), unidad, texto, documento, pagina, evidencia.",
           "Documento: id (ej. '20.500.12748/608'), titulo, fuente (INAIGEM|ANA|SENAMHI), anio.",
           "Tipos y cantidad: " + ", ".join(f"{a} {b}" for a, b in zip(n.tipo, n.n)),
           "RELACIONES (todas guardan pred, documento, pagina, evidencia, confianza, n_docs):"]
    lin += [f"  (:{a})-[:{t}]->(:{b})   pred={p} · {c}" for t, p, a, b, c in zip(r.rel, r.pred, r.desde, r.hacia, r.n)]
    return "\n".join(lin)


# ======================================================================
# 3. Enlace de entidades
# ======================================================================
class EnlazadorEntidades:
    def __init__(self, catalogo):
        c = catalogo.copy()
        c["k"] = c.k.fillna(c.nombre.map(norm))
        c = c[(c.k.str.len() >= 4) & ~c.dudoso.astype(bool) & ~c.k.isin(GENERICAS | NO_ENTIDAD) & ~c.tipo.isin(TIPOS_SIN_ENLACE)]
        c = c.sort_values("n_docs", ascending=False)
        self.cat = c
        self.por_k = {k: g for k, g in c.groupby("k", sort=False)}
        self.max_pal = int(c.k.str.count(" ").max() or 0) + 1

    def enlazar(self, pregunta, max_n=4):
        patron = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?"
        orig = re.findall(patron, sin_tildes(pregunta))
        toks = [t.lower() for t in orig]
        usados, hallados = set(), []
        for n in range(min(self.max_pal, 5), 0, -1):
            for i in range(len(toks) - n + 1):
                if any(j in usados for j in range(i, i + n)):
                    continue
                k = " ".join(toks[i:i + n])
                if k not in self.por_k:
                    continue
                g = self.por_k[k]
                pista = PISTAS_TIPO.get(toks[i - 1]) if i > 0 else None
                # un nombre propio: con mayúscula, precedido de "laguna", "nevado"…, o con dígitos (las palabras comunes ya se excluyeron)
                propio = orig[i][:1].isupper() or pista is not None or bool(re.search(r"\d", k))
                if not propio:
                    continue
                if pista and (g.tipo == pista).any():
                    g = g[g.tipo == pista]
                fila = g.iloc[0]
                hallados.append({"id": fila.id, "nombre": fila.nombre, "tipo": fila.tipo, "k": k, "pos": i})
                usados.update(range(i, i + n))
        if len(hallados) < max_n:   # nombres con tilde o letra distinta (Huascaran, Palcacoha…)
            try:
                from rapidfuzz import process, fuzz
                propios = [norm(w) for w in re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{4,}\b", pregunta)]
                ya = {h["k"] for h in hallados}
                for w in propios:
                    if w in ya or w in GENERICAS or w in NO_ENTIDAD or w in self.por_k:
                        continue
                    m = process.extractOne(w, list(self.por_k), scorer=fuzz.ratio, score_cutoff=88)
                    if m:
                        fila = self.por_k[m[0]].iloc[0]
                        hallados.append({"id": fila.id, "nombre": fila.nombre, "tipo": fila.tipo, "k": m[0], "pos": 99})
            except ImportError:
                pass
        hallados.sort(key=lambda h: h["pos"])
        return hallados[:max_n]


# ======================================================================
# 4. Instrucciones para el LLM
# ======================================================================
INSTRUCCIONES_GR = """Eres el asistente GraphRAG de Enterprise Brain Glaciares: respondes sobre glaciares, lagunas glaciares y riesgo de desborde en el Perú con documentos del INAIGEM, la ANA y el SENAMHI.

Recibes evidencia numerada de dos tipos:
- HECHOS DEL GRAFO: relaciones extraídas de los documentos y resultados de consultas al grafo de conocimiento (listas, conteos, cadenas de varios saltos). Cada hecho dice de qué documento y página sale.
- FRAGMENTOS DE TEXTO: pasajes de los documentos.

Reglas:
1. Usa SOLO esa evidencia. No uses conocimiento propio.
2. Termina cada oración que contenga un dato con su cita entre corchetes simples, por ejemplo [2] o [1][5] (no uses 【】). En listas, cada viñeta lleva su cita.
3. Para listas, conteos y relaciones entre entidades apóyate en los hechos del grafo; para explicar causas o contexto, en los fragmentos.
4. Si la evidencia dice que hay más filas de las mostradas, dilo (por ejemplo: "se muestran 25 de 80").
5. Copia las cifras y los nombres tal como aparecen. No sumes ni calcules si el resumen no lo trae.
6. Si dos fuentes se contradicen o son de años distintos, dilo e indica el año de cada una.
7. Responde siempre en español, aunque la evidencia esté en inglés.
8. Si hay hechos del grafo relacionados con la pregunta, preséntalos aunque cumplan solo parte de la condición y aclara qué parte cumplen (por ejemplo: "retroceso reportado" en lugar de "medido").
   Solo si no hay ninguna evidencia útil, responde exactamente: "No encontré esa información en el grafo ni en los documentos del corpus." Si solo permite una respuesta parcial, responde lo que puedas y di qué falta.
10. Sé claro y breve: máximo {palabras} palabras. Usa viñetas para las listas. No repitas la pregunta."""

INSTRUCCIONES_RUTA = """Eres el enrutador de un sistema GraphRAG sobre glaciares y lagunas del Perú. Elige UNA herramienta para obtener la evidencia estructurada del grafo.

Herramientas:
{catalogo}
- ficha: todo lo que el grafo sabe de una o más entidades nombradas en la pregunta (relaciones con documento y página).
- camino: cómo se conectan dos entidades nombradas (parámetros: a, b).
- cypher: ninguna consulta anterior sirve y la pregunta pide listas, conteos, filtros o relaciones de varios saltos; se escribirá una consulta Cypher nueva.
- texto: la pregunta es explicativa (por qué, cómo funciona, qué factores) y basta con fragmentos de texto.

Elige una consulta verificada SOLO si responde la pregunta con los mismos filtros. Si la pregunta agrega condiciones que la
consulta no tiene (otro departamento, una obra concreta, un año, una institución…), elige "cypher".

Entidades reconocidas en la pregunta: {entidades}

Responde SOLO con un JSON, sin texto adicional:
{{"herramienta": "<id>", "parametros": {{...}}, "motivo": "<una frase>"}}
Parámetros posibles: departamento (con tilde, ej. "Áncash"), nivel ("Muy alto"|"Alto"), niveles (lista), cordillera (minúsculas sin tildes, ej. "blanca"), anio (entero), hasta (entero), a y b (nombres de entidades)."""

INSTRUCCIONES_CYPHER = """Escribe UNA consulta Cypher de SOLO LECTURA para Neo4j 5 que responda la pregunta con este esquema:

{esquema}

Significado de las relaciones (respeta el sentido de la flecha):
- (Laguna)-[:ALIMENTADA_POR]->(Glaciar|Nevado): glaciar que alimenta la laguna.
- (Laguna)-[:AMENAZA_A]->(CentroPoblado): poblado aguas abajo expuesto a la laguna.
- (Laguna)-[:TIENE_NIVEL_RIESGO]->(NivelRiesgo {{nombre: 'Muy alto'|'Alto'|…}}).
- (Laguna)-[:TIENE_OBRA]->(ObraSeguridad {{nombre: 'Dique'|'Túnel de desagüe'|…}}); (Laguna)-[:ESTADO_OBRA {{obra}}]->(EstadoObra).
- (Institucion)-[:INTERVIENE_EN]->(Laguna|Glaciar|…): institución que estudió o intervino.
- (Laguna|Glaciar)-[:MONITOREADO_CON]->(MetodoMonitoreo); (Evento)-[:OCURRIO_EN]->(Laguna); (Evento)-[:AFECTO_A]->(CentroPoblado).
- (X)-[:UBICADA_EN]->(Cuenca|Cordillera|Distrito|Provincia|Departamento); (X)-[:PRESENTA_FENOMENO]->(Fenomeno).
- (X)-[:TIENE_MEDICION]->(Medicion {{propiedad, valor, unidad}}); (Documento)-[:MENCIONA]->(X).

Reglas:
- Usa MATCH, OPTIONAL MATCH, WHERE, WITH, UNWIND, RETURN, ORDER BY, LIMIT. Nunca CREATE, MERGE, SET, DELETE, REMOVE, DROP, LOAD CSV ni CALL.
- Filtra nombres con la propiedad k (minúsculas sin tildes): {{k: 'palcacocha'}}. Departamentos: 'Áncash' IN l.departamentos.
- Ignora relaciones de documentos (MENCIONA, USA_METODO, PUBLICADO_POR) salvo que la pregunta pida documentos.
- Devuelve columnas con nombres claros y, cuando existan, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia de la relación principal.
- Termina con LIMIT {limite} o menos.

Ejemplos:
{ejemplos}

Responde SOLO con la consulta, sin explicación ni bloque de código."""

# ======================================================================
# 4b. Cadena de LLMs con caché y modelo local de respaldo
# ======================================================================
class _Msg:
    def __init__(self, c):
        self.content = c


def _es_cuota(e):
    return bool(re.search(r"rate|429|quota|limit|exhausted|per day|TPD|RPD", str(e), re.I))


class CadenaLLM:
    """Varios LLM en fila. Límite por minuto → espera y reintenta; límite diario o error → pasa al siguiente.
    Guarda cada respuesta en un caché (archivo .jsonl): si la misma pregunta con la misma evidencia se repite, no gasta cuota."""

    def __init__(self, eslabones, ruta_cache=None, reintentos=5, log=print):
        self.eslabones = list(eslabones)          # [(nombre, llm)]; llm puede ser una función que lo crea (carga diferida)
        self.i, self.reintentos, self.log = 0, reintentos, log
        self.ultimo, self.uso = None, {}
        self.ruta_cache, self.cache = ruta_cache, {}
        if ruta_cache and os.path.exists(ruta_cache):
            for linea in open(ruta_cache, encoding="utf-8"):
                try:
                    x = json.loads(linea)
                    self.cache[x["k"]] = x
                except Exception:
                    pass

    @staticmethod
    def _clave(mensajes):
        import hashlib
        txt = json.dumps([(m[0], m[1]) if isinstance(m, tuple) else str(m) for m in (mensajes if isinstance(mensajes, list) else [mensajes])],
                         ensure_ascii=False)
        return hashlib.sha1(txt.encode("utf-8")).hexdigest()

    @property
    def nombre_actual(self):
        return self.eslabones[self.i][0] if self.i < len(self.eslabones) else "ninguno"

    def invoke(self, mensajes):
        k = self._clave(mensajes)
        if k in self.cache:
            self.ultimo = "caché · " + self.cache[k].get("modelo", "")
            self.uso["caché"] = self.uso.get("caché", 0) + 1
            return _Msg(self.cache[k]["r"])
        ultimo_error = None
        while self.i < len(self.eslabones):
            nombre, llm = self.eslabones[self.i]
            try:
                if callable(llm) and not hasattr(llm, "invoke"):
                    self.log(f"Cargando {nombre}…")
                    llm = llm()
                    self.eslabones[self.i] = (nombre, llm)
                t = _texto_llm(llm, mensajes, self.reintentos)
                self.ultimo = nombre
                self.uso[nombre] = self.uso.get(nombre, 0) + 1
                if self.ruta_cache:
                    reg = {"k": k, "r": t, "modelo": nombre}
                    self.cache[k] = reg
                    with open(self.ruta_cache, "a", encoding="utf-8") as f:
                        f.write(json.dumps(reg, ensure_ascii=False) + "\n")
                return _Msg(t)
            except Exception as e:
                ultimo_error = e
                motivo = "cuota agotada" if _es_cuota(e) else "error"
                self.i += 1
                self.log(f"⚠️ {nombre}: {motivo} ({str(e)[:90]}) → paso a {self.nombre_actual}")
        raise RuntimeError(f"Ningún LLM disponible: {str(ultimo_error)[:200]}")


def modelos_disponibles(proveedor, clave):
    """Lista los modelos de chat que ofrece hoy la cuenta (Groq o Gemini), para no depender de nombres fijos."""
    import requests
    try:
        if proveedor == "groq":
            r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {clave}"}, timeout=20)
            ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
            fuera = re.compile(r"whisper|tts|guard|playai|orpheus|compound|distil|prompt", re.I)
            return [i for i in ids if not fuera.search(i)]
        if proveedor == "gemini":
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": clave, "pageSize": 200}, timeout=20)
            ids = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                   if "generateContent" in m.get("supportedGenerationMethods", [])]
            return [i for i in ids if re.search(r"flash|pro", i) and not re.search(r"image|tts|audio|live|embedding|vision|exp|preview", i)]
    except Exception:
        return []
    return []


class LLMHuggingFace:
    """LLM descargado de Hugging Face que corre dentro de Colab (sin límites de uso). Se carga solo cuando hace falta."""

    def __init__(self, modelo="Qwen/Qwen2.5-7B-Instruct", max_tokens=700):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        kw = {"device_map": "auto", "torch_dtype": torch.float16}
        if "7B" in modelo or "8B" in modelo:
            try:
                from transformers import BitsAndBytesConfig
                kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16)
            except Exception:
                modelo = "Qwen/Qwen2.5-3B-Instruct"
        self.tok = AutoTokenizer.from_pretrained(modelo)
        self.m = AutoModelForCausalLM.from_pretrained(modelo, **kw)
        self.model_name, self.max_tokens = modelo, max_tokens

    def invoke(self, mensajes):
        import torch
        if isinstance(mensajes, str):
            mensajes = [("human", mensajes)]
        rol = {"system": "system", "human": "user", "ai": "assistant", "user": "user", "assistant": "assistant"}
        chat = [{"role": rol.get(r, "user"), "content": c} for r, c in mensajes]
        ids = self.tok.apply_chat_template(chat, add_generation_prompt=True, return_tensors="pt").to(self.m.device)
        with torch.no_grad():
            out = self.m.generate(ids, max_new_tokens=self.max_tokens, do_sample=False, pad_token_id=self.tok.eos_token_id)
        return _Msg(self.tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True))


PROHIBIDO = re.compile(r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV|CALL|FOREACH|ALTER|GRANT|DENY|REVOKE)\b", re.I)


def _texto_llm(llm, mensajes, reintentos=5):
    """Llama al LLM; si el plan gratuito pide esperar (429), espera lo que indica y reintenta."""
    for i in range(reintentos):
        try:
            r = llm.invoke(mensajes)
            c = r.content if hasattr(r, "content") else str(r)
            if isinstance(c, list):   # algunos modelos devuelven partes
                c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
            return re.sub(r"<think>.*?</think>", "", c or "", flags=re.S).strip()
        except Exception as e:
            if i == reintentos - 1 or re.search(r"per day|TPD|RPD|daily|exhausted", str(e), re.I):
                raise
            m = re.search(r"(?:try again in|retry in|retry_delay[^0-9]*)\s*([\d.]+)\s*(ms|s)?", str(e), re.I)
            if m:
                espera = float(m.group(1)) / (1000 if (m.group(2) or "").lower() == "ms" else 1) + 1
            else:
                espera = 10 * (i + 1) if re.search(r"rate|429|quota|limit|exhausted", str(e), re.I) else 2
            time.sleep(min(espera, 70))


def _json(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(re.sub(r",\s*}", "}", m.group(0).replace("'", '"')))
        except Exception:
            return None


def validar_cypher(c, limite=50):
    """Deja pasar solo consultas de lectura y les impone un LIMIT."""
    c = re.sub(r"^```(?:cypher)?|```$", "", (c or "").strip(), flags=re.I | re.M).strip().rstrip(";")
    if not re.search(r"\bMATCH\b", c, re.I) or not re.search(r"\bRETURN\b", c, re.I):
        raise ValueError("la consulta no tiene MATCH … RETURN")
    if PROHIBIDO.search(re.sub(r"'[^']*'|\"[^\"]*\"", "''", c)):
        raise ValueError("la consulta intenta modificar el grafo o llamar procedimientos")
    m = re.search(r"\bLIMIT\s+(\d+)\s*$", c, re.I)
    if not m:
        c += f"\nLIMIT {limite}"
    elif int(m.group(1)) > limite:
        c = c[:m.start(1)] + str(limite)
    return c


# ======================================================================
# 5. Motor GraphRAG
# ======================================================================
def _vacio(v):
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    if isinstance(v, (list, tuple, set)):
        return len(v) == 0
    return str(v).strip() in ("", "nan", "None", "[]")


def _fmt(v, n=160):
    if isinstance(v, (list, tuple)):
        v = ", ".join(_fmt(x, n) for x in v if not _vacio(x))
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"\s+", " ", str(v)).strip()
    s = re.sub(r"^(-?\d+)\.0$", r"\1", s)
    return s if len(s) <= n else s[:n - 1] + "…"


class GraphRAG:
    def __init__(self, grafo, indice=None, llm=None, R=None, config=None, log=print, llm_respaldo=None):
        self.g, self.indice, self.llm, self.R = grafo, indice, llm, R
        self.llm_respaldo = llm_respaldo
        self.cfg = {**CONFIG_GR, **(config or {})}
        self.log = log
        t0 = time.time()
        self.cat = grafo.catalogo()
        self.enl = EnlazadorEntidades(self.cat)
        self._esquema = None
        self._docs = {}
        log(f"GraphRAG listo · grafo: {grafo.nombre} · {len(self.cat):,} entidades enlazables · "
            f"texto: {'sí' if indice is not None else 'no'} · LLM: {'sí' if llm is not None else 'no (respaldo)'} · "
            f"{time.time() - t0:.0f} s")

    # ---------- utilidades ----------
    def _llm(self, mensajes):
        """Llama al LLM principal; si se agota su cuota, pasa al de respaldo (por ejemplo, de Groq a Gemini) y se queda con él."""
        try:
            return _texto_llm(self.llm, mensajes, self.cfg["REINTENTOS_LLM"])
        except Exception as e:
            if self.llm_respaldo is None or not re.search(r"rate|429|quota|limit|exhausted", str(e), re.I):
                raise
            self.log(f"⚠️ Cuota del LLM principal agotada → se usa el LLM de respaldo ({str(e)[:90]}…)")
            self.llm, self.llm_respaldo = self.llm_respaldo, None
            return _texto_llm(self.llm, mensajes, self.cfg["REINTENTOS_LLM"])

    @property
    def esquema(self):
        if self._esquema is None:
            self._esquema = self.g.esquema_texto()
        return self._esquema

    def titulos(self, ids):
        faltan = [i for i in set(ids) if i and i not in self._docs]
        if faltan:
            try:
                for r in self.g.documentos(faltan).itertuples():
                    self._docs[r.id] = {"titulo": _fmt(r.titulo or r.id, 120), "fuente": r.fuente, "anio": r.anio}
            except Exception:
                pass
        return self._docs

    def _ref_doc(self, doc, pagina=None):
        d = self._docs.get(doc) or {}
        extra = [_fmt(x) for x in (d.get("fuente"), d.get("anio")) if not _vacio(x)]
        if not _vacio(pagina):
            extra.append(f"p. {_fmt(pagina)}")
        return (d.get("titulo") or str(doc)) + (" — " + ", ".join(extra) if extra else "")

    # ---------- paso 2: ruta ----------
    def parametros(self, pregunta, entidades):
        q = norm(pregunta)
        p = {}
        dep = [DEPARTAMENTOS_N[k] for k in DEPARTAMENTOS_N if re.search(rf"\b{k}\b", q)]
        if dep:
            p["departamento"] = "Áncash" if norm(dep[0]) == "ancash" else dep[0]
        if "muy alto" in q and not re.search(r"\balto (o|y) muy alto|muy alto (o|y) alto", q):
            p["nivel"], p["niveles"] = "Muy alto", ["Muy alto"]
        elif re.search(r"\balto (o|y) muy alto|muy alto (o|y) alto", q):
            p["niveles"] = ["Muy alto", "Alto"]
        cord = [e["k"] for e in entidades if e["tipo"] == "Cordillera"]
        if not cord:
            conocidas = set(self.cat[self.cat.tipo == "Cordillera"].k) if "k" in self.cat else set()
            cord = [c for c in re.findall(r"cordillera (\w+(?: \w+)?)", q) if c in conocidas or c.split()[0] in conocidas]
            cord = [c if c in conocidas else c.split()[0] for c in cord]
        if cord:
            p["cordillera"] = cord[0]
        anios = [int(a) for a in re.findall(r"\b(19\d\d|20\d\d)\b", q)]
        if anios:
            p["anio"] = max(anios)
            if re.search(r"antes de", q):
                p["hasta"] = anios[0]
        if len(entidades) >= 2:
            p["a"], p["b"] = entidades[0]["k"], entidades[1]["k"]
        return p

    def ruta_reglas(self, pregunta, entidades):
        q = norm(pregunta)
        if re.search(r"(c[oó]mo|qu[eé] relaci[oó]n).*(se (relaciona|conecta)|entre|y)", q) and len(entidades) >= 2 \
                and re.search(r"relacion|conect|vincul", q):
            return "camino", "dos entidades y la pregunta pide su conexión"
        if re.search(norm(PALABRAS_TEXTO), q):
            return ("ficha" if entidades else "texto"), "pregunta explicativa"
        for k, pats in REGLAS_RUTA:
            if all(re.search(norm(p), q) for p in pats):
                return k, f"palabras clave de {k}"
        if entidades:
            return "ficha", "entidades reconocidas"
        return "texto", "sin entidades ni consulta aplicable"

    def ruta(self, pregunta, entidades):
        herr, motivo, origen = *self.ruta_reglas(pregunta, entidades), "reglas"
        regla = herr
        params = {}
        reconocida = regla in PLANTILLAS or regla == "camino"
        if self.llm is not None and (self.cfg["RUTA_LLM_SIEMPRE"] or not reconocida):
            cat = "\n".join(f"- {k}: {v}" for k, v in ROTULOS.items())
            ents = ", ".join(f"{e['nombre']} ({e['tipo']})" for e in entidades) or "ninguna"
            msg = [("system", INSTRUCCIONES_RUTA.format(catalogo=cat, entidades=ents)), ("human", pregunta)]
            try:
                j = _json(self._llm(msg))
                validas = set(PLANTILLAS) | {"ficha", "camino", "cypher", "texto"}
                if j and j.get("herramienta") in validas:
                    herr, motivo, origen = j["herramienta"], j.get("motivo", ""), "LLM"
                    params = j.get("parametros") or {}
            except Exception as e:
                motivo += f" (enrutador LLM no disponible: {str(e)[:80]})"
        if herr == "cypher" and not (self.g.cypher_ok and getattr(self.g, "cypher_libre_ok", True)):
            herr = regla if regla in PLANTILLAS and self.g.cypher_ok else ("ficha" if entidades else "texto")
        if herr in PLANTILLAS and not self.g.cypher_ok:
            herr = "ficha" if entidades else "texto"
        if herr == "camino" and len(entidades) < 2:
            herr = "ficha" if entidades else "texto"
        if herr == "ficha" and not entidades:
            herr = "texto"
        return {"herramienta": herr, "motivo": motivo, "origen": origen, "parametros_llm": params}

    # ---------- paso 2b: ejecutar ----------
    def ejecutar(self, plan, pregunta, entidades):
        h = plan["herramienta"]
        auto = self.parametros(pregunta, entidades)
        if h in PLANTILLAS:
            c = PLANTILLAS[h]
            params = dict(c.get("params", {}))
            for k_ in params:
                v = plan["parametros_llm"].get(k_, auto.get(k_))
                if v is None:
                    continue
                if k_ == "cordillera":
                    v = norm(v).replace("cordillera ", "")
                if k_ == "departamento":
                    v = "Áncash" if norm(v) == "ancash" else str(v)
                if k_ == "niveles" and isinstance(v, str):
                    v = [v]
                if k_ in ("anio", "hasta"):
                    v = int(v)
                params[k_] = v
            plan.update(cypher=c["cypher"].strip(), parametros=params, consulta=c["titulo"])
            d = self.g.q(c["cypher"], **params)
            if h == "P9" and len(d):
                plan["resumen_calculado"] = comparar_superficie(d).to_dict("records")
            return d
        if h == "ficha":
            ids = [e["id"] for e in entidades]
            plan.update(parametros={"entidades": [e["nombre"] for e in entidades]})
            return self.g.ficha(ids, self.cfg["MAX_HECHOS_FICHA"])
        if h == "camino":
            a, b = entidades[0], entidades[1]
            plan.update(parametros={"a": a["nombre"], "b": b["nombre"]})
            return self.g.camino(a["id"], b["id"])
        if h == "cypher":
            return self.text2cypher(pregunta, plan)
        return pd.DataFrame()

    def text2cypher(self, pregunta, plan):
        ejemplos = "\n\n".join(f"Pregunta: {PLANTILLAS[k]['titulo']}\n{PLANTILLAS[k]['cypher'].strip()}" for k in ("P2", "P6", "E3_conteo_departamento"))
        sistema = INSTRUCCIONES_CYPHER.format(esquema=self.esquema, limite=self.cfg["LIMIT_CYPHER"], ejemplos=ejemplos)
        msg = [("system", sistema), ("human", pregunta)]
        ultimo_error, previa = None, ""
        for intento in range(2):
            if ultimo_error:
                msg = msg[:2] + [("ai", previa), ("human", f"Esa consulta falló: {ultimo_error}. Corrígela y responde solo con la consulta.")]
            try:
                previa = self._llm(msg)
                c = validar_cypher(previa, self.cfg["LIMIT_CYPHER"])
                plan.update(cypher=c, parametros={}, consulta="Text2Cypher (consulta escrita por el LLM y validada)")
                d = self.g.q(c)
                plan["intentos_cypher"] = intento + 1
                return d
            except Exception as e:
                ultimo_error = str(e)[:300]
        plan["error_cypher"] = ultimo_error
        return pd.DataFrame()

    # ---------- paso 2c: hechos numerados ----------
    def hechos(self, d, plan):
        """Convierte el resultado del grafo en evidencia numerada (una fila = un hecho citable)."""
        if d is None or not len(d):
            return pd.DataFrame(columns=["texto", "documento", "pagina", "evidencia"]), {}
        d = d.copy()
        for base in ("documento", "pagina", "evidencia"):   # Text2Cypher a veces escribe documento_amenaza, pagina_x…
            if base not in d:
                alt = next((c for c in d.columns if re.match(rf"^{base}_", str(c))), None)
                if alt is not None:
                    d[base] = d[alt]
        cols_ev = [c for c in d.columns if re.match(r"^(documento|pagina|evidencia)(_|$)", str(c))]
        # documentos mencionados en columnas de listas (estudios, documentos)
        docs = set(d["documento"].dropna().astype(str)) if "documento" in d else set()
        for c in ("documentos", "estudios"):
            if c in d:
                for v in d[c]:
                    if isinstance(v, (list, tuple)):
                        docs.update(str(x) for x in v if not _vacio(x))
        self.titulos(docs)
        resumen = {"filas": len(d)}
        prim = next((c for c in d.columns if c not in cols_ev), None)
        if prim is not None:
            try:
                resumen[f"{prim}_distintos"] = int(d[prim].map(lambda v: _fmt(v)).nunique())
            except Exception:
                pass
        if plan["herramienta"] == "ficha" and "n_docs" in d:
            d = d.sort_values("n_docs", ascending=False)
        d = d.head(self.cfg["MAX_FILAS"])
        filas = []
        for r in d.to_dict("records"):
            if plan["herramienta"] == "ficha":
                flecha = f"{r['entidad']} —{r['relacion']}→ {r['con']}" if r.get("sentido") == "sale" else f"{r['con']} —{r['relacion']}→ {r['entidad']}"
                texto = f"{flecha} ({r.get('tipo_con')})"
                if not _vacio(r.get("n_docs")):
                    texto += f" · en {_fmt(r['n_docs'])} documento(s)"
            elif plan["herramienta"] == "camino":
                texto = f"Paso {r['paso']}: {r['desde']} —{r['relacion']}→ {r['hacia']}"
            else:
                texto = " · ".join(f"{k}: {_fmt(v)}" for k, v in r.items() if k not in cols_ev and not _vacio(v))
            doc, ev = r.get("documento"), r.get("evidencia")
            if _vacio(doc):
                lista = r.get("documentos") if isinstance(r.get("documentos"), (list, tuple)) else r.get("estudios")
                doc = next((x for x in (lista or []) if not _vacio(x)), None) if isinstance(lista, (list, tuple)) else None
            filas.append({"texto": texto, "documento": doc, "pagina": r.get("pagina"),
                          "evidencia": _fmt(ev, 220) if not _vacio(ev) else ""})
        return pd.DataFrame(filas), resumen

    # ---------- paso 3: texto guiado por el grafo ----------
    def fragmentos(self, pregunta, entidades, docs_grafo):
        if self.indice is None:
            return pd.DataFrame(), None
        consulta = pregunta + (" " + " ".join(e["nombre"] for e in entidades) if entidades else "")
        try:
            d = self.indice.buscar(consulta, k=max(20, 4 * self.cfg["K_TEXTO"]), modo="hibrido")
        except TypeError:
            d = self.indice.buscar(consulta, k=max(20, 4 * self.cfg["K_TEXTO"]))
        if d is None or not len(d):
            return pd.DataFrame(), None
        d = d.reset_index(drop=True).copy()
        sim_max = float(pd.to_numeric(d.get("sim"), errors="coerce").max()) if "sim" in d else None
        pid = d["paper_id"].astype(str) if "paper_id" in d else pd.Series([""] * len(d))
        nombres = [norm(e["nombre"]) for e in entidades]
        txt = d["text"] if "text" in d else d.get("texto", pd.Series([""] * len(d)))
        d["_punt"] = (1.0 / (d.index + 1)) + 0.5 * pid.isin(docs_grafo).astype(float) \
            + 0.2 * txt.map(lambda t: any(n in norm(t) for n in nombres)).astype(float)
        d = d.sort_values("_punt", ascending=False)
        d["_doc"] = pid
        d = d.groupby("_doc", sort=False).head(self.cfg["MAX_POR_DOC"]).head(self.cfg["K_TEXTO"])
        filas = []
        for f in d.itertuples():
            try:
                ref = self.R.referencia(f) if self.R is not None else ""
            except Exception:
                ref = ""
            texto = getattr(f, "text", None) or getattr(f, "texto", "")
            filas.append({"texto": re.sub(r"\s+", " ", str(texto)).strip()[:700], "referencia": ref or str(getattr(f, "paper_id", "")),
                          "paper_id": str(getattr(f, "paper_id", "")), "guiado_por_grafo": str(getattr(f, "paper_id", "")) in docs_grafo})
        return pd.DataFrame(filas), sim_max

    # ---------- paso 4: respuesta ----------
    def responder(self, pregunta, ver_plan=True):
        t0 = time.time()
        pregunta = pregunta.strip()
        entidades = self.enl.enlazar(pregunta)
        regla = self.ruta_reglas(pregunta, entidades)[0]
        if not entidades and regla not in PLANTILLAS and self.indice is not None:
            try:
                umbral = getattr(self.indice, "cfg", {}).get("UMBRAL_SIM")
                s0 = float(self.indice.buscar(pregunta, k=1, modo="semantico").sim.iloc[0])
                if umbral is not None and s0 < umbral:
                    plan = {"herramienta": "texto", "motivo": f"fuera del corpus (similitud {s0:.2f} < {umbral})", "origen": "umbral",
                            "parametros_llm": {}, "entidades": [], "filas_grafo": 0}
                    return {"pregunta": pregunta, "respuesta": "No encontré esa información en el grafo ni en los documentos del corpus.",
                            "plan": plan, "hechos": pd.DataFrame(columns=["texto", "documento", "pagina", "evidencia"]),
                            "fragmentos": pd.DataFrame(), "fuentes": [], "citadas": [], "n_citas": 0, "n_citas_grafo": 0,
                            "n_citas_texto": 0, "docs_citados": [], "instituciones": [],
                            "avisos": ["La pregunta no se parece a nada del corpus ni a ninguna entidad del grafo (no se llamó al LLM)."],
                            "modo": "LLM", "modelo": None, "autocorregida": False, "sim_max": s0,
                            "segundos": round(time.time() - t0, 1), "contextos": []}
            except Exception:
                pass
        plan = self.ruta(pregunta, entidades)
        plan["entidades"] = [f"{e['nombre']} ({e['tipo']})" for e in entidades]
        try:
            d = self.ejecutar(plan, pregunta, entidades)
        except Exception as e:
            plan["error"] = str(e)[:300]
            d = pd.DataFrame()
        if (d is None or not len(d)) and plan["herramienta"] not in ("ficha", "texto") and entidades:
            plan["respaldo"] = f"{plan['herramienta']} no devolvió filas → ficha de las entidades"
            d = self.g.ficha([e["id"] for e in entidades], self.cfg["MAX_HECHOS_FICHA"])
            plan["herramienta_final"] = "ficha"
            plan_f = {**plan, "herramienta": "ficha"}
        else:
            plan_f = plan
        H, resumen = self.hechos(d, plan_f)
        plan["filas_grafo"] = resumen.get("filas", 0)
        docs_grafo = set(H["documento"].dropna().astype(str)) if len(H) else set()
        T, sim_max = self.fragmentos(pregunta, entidades, docs_grafo)
        umbral = getattr(self.indice, "cfg", {}).get("UMBRAL_SIM") if self.indice is not None else None

        # evidencia numerada: primero el grafo, luego el texto
        fuentes, bloques = [], []
        for r in H.itertuples():
            n = len(fuentes) + 1
            ref = self._ref_doc(r.documento, r.pagina) if not _vacio(r.documento) else f"Grafo de conocimiento · {plan.get('consulta', plan['herramienta'])}"
            fuentes.append({"n": n, "tipo": "grafo", "texto": r.texto, "referencia": ref,
                            "documento": None if _vacio(r.documento) else str(r.documento), "evidencia": r.evidencia})
            bloques.append(f"[{n}] (grafo) {r.texto}" + (f"\n    evidencia: \"{r.evidencia}\"" if r.evidencia else "") + f"\n    fuente: {ref}")
        for r in T.itertuples():
            n = len(fuentes) + 1
            fuentes.append({"n": n, "tipo": "texto", "texto": r.texto, "referencia": r.referencia, "documento": r.paper_id or None,
                            "evidencia": "", "guiado_por_grafo": r.guiado_por_grafo})
            bloques.append(f"[{n}] (texto) {r.referencia}\n{r.texto}")

        modo, modelo_usado, autocorregida = "LLM", None, False
        fuera = (not len(H)) and (sim_max is not None and umbral is not None and sim_max < umbral)
        if fuera or not fuentes:
            respuesta = "No encontré esa información en el grafo ni en los documentos del corpus."
            avisos = ["La pregunta no se parece a nada del corpus ni a ninguna entidad del grafo."] if fuera else []
        elif self.llm is None:
            respuesta = self._respaldo(H, T, resumen)
            avisos = ["Modo de respaldo: sin LLM se muestran los hechos y fragmentos encontrados."]
            modo = "respaldo"
        else:
            extra = ""
            if resumen.get("filas", 0) > len(H):
                extra += f"\nRESUMEN: la consulta al grafo devolvió {resumen['filas']} filas; se muestran {len(H)}."
            if plan.get("resumen_calculado"):
                extra += f"\nRESUMEN CALCULADO (P9): {json.dumps(plan['resumen_calculado'], ensure_ascii=False, default=str)}"
            for k_, v in resumen.items():
                if k_.endswith("_distintos"):
                    extra += f"\nValores distintos de '{k_[:-10]}' en el resultado completo: {v}."
            contexto = ("HECHOS DEL GRAFO Y FRAGMENTOS (evidencia numerada):\n\n" + "\n\n".join(bloques) + "\n" + extra +
                        f"\n\nPREGUNTA: {pregunta}")
            sistema = INSTRUCCIONES_GR.format(palabras=self.cfg["PALABRAS"])
            try:
                mensajes = [("system", sistema), ("human", contexto)]
                respuesta = normalizar_citas(self._llm(mensajes).strip())
                avisos = verificar(respuesta, fuentes, contexto)
                modelo_usado = getattr(self.llm, "ultimo", None)
                graves = [a for a in avisos if a.startswith(("Citas que no existen", "Cifras que no aparecen"))
                          or re.match(r"([2-9]|\d\d+) oración", a)]
                if graves and self.cfg["AUTOCORRECCION"]:
                    pedido = ("Revisa tu respuesta. Problemas encontrados: " + " ".join(graves) +
                              " Corrígela: cada oración con un dato termina con su cita [n] y usa solo cifras que aparecen en la evidencia."
                              " Devuelve solo la respuesta corregida.")
                    try:
                        r2 = normalizar_citas(self._llm(mensajes + [("ai", respuesta), ("human", pedido)]).strip())
                        av2 = verificar(r2, fuentes, contexto)
                        valida = not r2.startswith("No encontré") and len(re.findall(r"\[\d+\]", r2)) >= min(1, len(re.findall(r"\[\d+\]", respuesta)))
                        if valida and (len(av2) < len(avisos) or (len(av2) == len(avisos) and len(" ".join(av2)) < len(" ".join(avisos)))):
                            respuesta, avisos, autocorregida = r2, av2, True
                            modelo_usado = getattr(self.llm, "ultimo", modelo_usado)
                    except Exception:
                        pass
            except Exception as e:
                respuesta = self._respaldo(H, T, resumen)
                avisos = [f"El LLM no respondió ({str(e)[:120]}): se muestra la evidencia."]
                modo = "respaldo"
        citadas = sorted({int(x) for x in re.findall(r"\[(\d+)\]", respuesta) if 0 < int(x) <= len(fuentes)})
        usadas = [f for f in fuentes if f["n"] in citadas]
        docs = sorted({f["documento"] for f in usadas if f["documento"]})
        self.titulos(docs)
        inst = sorted({str((self._docs.get(x) or {}).get("fuente")) for x in docs if (self._docs.get(x) or {}).get("fuente")})
        return {"pregunta": pregunta, "respuesta": respuesta, "plan": plan, "hechos": H, "fragmentos": T, "fuentes": fuentes,
                "citadas": citadas, "n_citas": len(citadas), "n_citas_grafo": sum(f["tipo"] == "grafo" for f in usadas),
                "n_citas_texto": sum(f["tipo"] == "texto" for f in usadas), "docs_citados": docs, "instituciones": inst,
                "avisos": avisos, "modo": modo, "modelo": modelo_usado, "autocorregida": autocorregida, "sim_max": sim_max, "segundos": round(time.time() - t0, 1),
                "contextos": [b for b in bloques]}

    def _respaldo(self, H, T, resumen):
        lin = []
        if len(H):
            lin.append(f"**Hechos del grafo** ({resumen.get('filas', len(H))} filas):")
            lin += [f"- {t} [{i + 1}]" for i, t in enumerate(H.texto)]
        if len(T):
            lin.append("\n**Fragmentos relacionados:**")
            lin += [f"- {t[:220]}… [{len(H) + i + 1}]" for i, t in enumerate(T.texto)]
        return "\n".join(lin)


# ======================================================================
# 6. Verificación y presentación
# ======================================================================
def normalizar_citas(t):
    """Algunos modelos citan con 【3】, [3†L5] o [1, 2]: se pasa todo a [3] y [1][2]."""
    t = re.sub(r"【\s*(\d+)[^】]*】", r"[\1]", t or "")
    t = re.sub(r"\[(\d+)†[^\]]*\]", r"[\1]", t)
    t = re.sub(r"\[(\d+(?:\s*[,;]\s*\d+)+)\]", lambda m: "".join(f"[{x}]" for x in re.split(r"\s*[,;]\s*", m.group(1))), t)
    return t


def _numeros(t):
    # "1 250,5" o "1.250" son un solo número (miles con separador de 3 cifras); "1932 1941" son dos años distintos
    pat = r"\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d+)?(?!\d)|\d+(?:[.,]\d+)?"
    return {re.sub(r"[ .,]", "", x) for x in re.findall(pat, t) if len(re.sub(r"\D", "", x)) >= 2}


def verificar(respuesta, fuentes, contexto):
    avisos = []
    if respuesta.startswith("No encontré"):
        return avisos
    if fuentes and not re.search(r"\[\d+\]", respuesta):
        avisos.append("Citas que no existen: la respuesta no cita ninguna fuente.")
    malas = sorted({int(x) for x in re.findall(r"\[(\d+)\]", respuesta) if not 0 < int(x) <= len(fuentes)})
    if malas:
        avisos.append(f"Citas que no existen: {malas}.")
    oraciones = [o for o in re.split(r"(?<=[.!?])\s+|\n+", respuesta) if o.strip()]
    ctx_n = _numeros(contexto)
    # solo cuenta una oración sin cita si trae cifras que NO están en la evidencia (las que sí están son datos respaldados)
    sin = [o for o in oraciones if not re.search(r"\[\d+\]", o)
           and (lambda ns: ns and not all(n in ctx_n or any(n in c for c in ctx_n) for n in ns))(_numeros(o))]
    if sin:
        avisos.append(f"{len(sin)} oración(es) con datos sin cita.")
    ctx = _numeros(contexto)
    raros = sorted(n for n in _numeros(re.sub(r"\[\d+\]", "", respuesta)) if n not in ctx and not any(n in c for c in ctx))
    if raros:
        avisos.append(f"Cifras que no aparecen en la evidencia: {', '.join(raros[:6])}.")
    return avisos


def a_markdown(r, ver_evidencia=False):
    p = r["plan"]
    lin = [f"**Pregunta:** {r['pregunta']}", "", r["respuesta"], ""]
    herr = p.get("herramienta_final") or p["herramienta"]
    lin.append(f"**🧭 Cómo se respondió:** herramienta `{p['herramienta']}`" + (f" → `{herr}`" if herr != p["herramienta"] else "")
               + f" (elegida por {p.get('origen')}: {p.get('motivo', '')}) · entidades: {', '.join(p.get('entidades', [])) or '—'}"
               + f" · filas del grafo: {p.get('filas_grafo', 0)} · fragmentos: {len(r['fragmentos'])}")
    if p.get("consulta"):
        lin.append(f"<sub>Consulta: {p['consulta']}</sub>")
    if p.get("respaldo"):
        lin.append(f"<sub>{p['respaldo']}</sub>")
    if p.get("cypher"):
        lin += ["", "<details><summary>Ver la consulta Cypher</summary>", "", "```cypher", p["cypher"], "```",
                f"Parámetros: `{json.dumps(p.get('parametros', {}), ensure_ascii=False)}`", "</details>"]
    if p.get("error_cypher") or p.get("error"):
        lin.append(f"> ⚠️ {p.get('error_cypher') or p.get('error')}")
    usadas = [f for f in r["fuentes"] if f["n"] in r["citadas"]]
    if usadas:
        lin += ["", "**Fuentes**"]
        for f in usadas:
            icono = "🕸️ grafo" if f["tipo"] == "grafo" else "📄 texto"
            enlace = f" · [ficha](https://hdl.handle.net/{f['documento']})" if f.get("documento") and re.match(r"^\d+\.\d+/", f["documento"]) else ""
            detalle = f" · {f['texto'][:140]}" if f["tipo"] == "grafo" else ""
            lin.append(f"- [{f['n']}] {icono} · {f['referencia']}{enlace}{detalle}")
    for a in r["avisos"]:
        lin.append(f"\n> ⚠️ {a}")
    extra = f" · modelo: {r.get('modelo')}" if r.get("modelo") else ""
    extra += " · corregida tras la verificación" if r.get("autocorregida") else ""
    lin.append(f"\n<sub>{r['segundos']} s · citas del grafo: {r['n_citas_grafo']} · citas de texto: {r['n_citas_texto']}{extra}</sub>")
    if ver_evidencia:
        lin += ["", "**Evidencia que recibió el LLM**", ""] + [f"- [{f['n']}] ({f['tipo']}) {f['texto'][:200]}" for f in r["fuentes"]]
    return "\n".join(lin)


# ======================================================================
# 7. Evaluación (mismo banco de la semana 9) y comparación RAG vs GraphRAG
# ======================================================================
def evaluar(banco, motor, pausa=2.0, log=print):
    filas, ragas = [], []
    for i, b in enumerate(banco):
        r = motor.responder(b["pregunta"])
        responde = r["modo"] == "LLM" and not r["respuesta"].startswith("No encontré")
        filas.append({"id": b["id"], "tipo": b.get("tipo", ""), "pregunta": b["pregunta"].strip(), "responde": responde,
                      "modo": r["modo"], "modelo": r.get("modelo"), "autocorregida": r.get("autocorregida", False), "herramienta": r["plan"].get("herramienta_final") or r["plan"]["herramienta"],
                      "filas_grafo": r["plan"].get("filas_grafo", 0), "n_citas": r["n_citas"], "citas_grafo": r["n_citas_grafo"],
                      "citas_texto": r["n_citas_texto"], "docs_citados": len(r["docs_citados"]),
                      "instituciones_citadas": ", ".join(r["instituciones"]), "avisos": " ".join(r["avisos"]),
                      "segundos": r["segundos"], "respuesta": r["respuesta"], "cypher": r["plan"].get("cypher", "")})
        herr = filas[-1]["herramienta"]
        ref = ""
        if (herr in PLANTILLAS or herr == "camino") and len(r["hechos"]):
            ref = "\n".join(f"- {t}" for t in r["hechos"].texto.head(15))   # respuesta de referencia: hechos del grafo verificado
        ragas.append({"id": b["id"], "question": b["pregunta"].strip(), "answer": r["respuesta"],
                      "reference": ref, "reference_source": f"grafo · {herr}" if ref else "",
                      "contexts": [f["texto"] + (f" | evidencia: {f['evidencia']}" if f.get("evidencia") else "") for f in r["fuentes"]],
                      "sources": [f["referencia"] for f in r["fuentes"]], "tool": herr, "model": r.get("modelo")})
        log(f"{b['id']:>4} · {filas[-1]['herramienta']:<26} · {'✅' if responde else ('⚠️ respaldo' if r['modo'] != 'LLM' else '❌')} · citas {r['n_citas']} "
            f"(grafo {r['n_citas_grafo']}) · {r['segundos']} s · {r.get('modelo') or '—'}" + (" · corregida" if r.get("autocorregida") else ""))
        if pausa and i < len(banco) - 1 and not str(r.get("modelo") or "").startswith("caché"):
            time.sleep(pausa)
    return pd.DataFrame(filas), ragas


def comparar(eval_gr, ruta_rag_csv):
    """Une los resultados del GraphRAG con los del RAG de la semana 9 (respuestas_rag.csv)."""
    if not ruta_rag_csv or not os.path.exists(ruta_rag_csv):
        return None
    rag = pd.read_csv(ruta_rag_csv)
    rag = rag[[c for c in ("id", "responde", "n_citas", "docs_citados", "avisos") if c in rag]]
    rag = rag.rename(columns={c: f"{c}_rag" for c in rag.columns if c != "id"})
    gr = eval_gr[["id", "tipo", "herramienta", "modelo", "filas_grafo", "responde", "n_citas", "citas_grafo", "docs_citados", "avisos"]]
    gr = gr.rename(columns={c: f"{c}_graphrag" for c in ("responde", "n_citas", "docs_citados", "avisos")})
    return rag.merge(gr, on="id", how="right")[["id", "tipo", "responde_rag", "responde_graphrag", "n_citas_rag", "n_citas_graphrag",
                                                  "citas_grafo", "docs_citados_rag", "docs_citados_graphrag", "herramienta", "modelo",
                                                  "filas_grafo", "avisos_rag", "avisos_graphrag"]]


# ======================================================================
# 8. Vista del subgrafo que respalda una respuesta (Pyvis, sin internet)
# ======================================================================
COLORES = {"Laguna": "#2a9df4", "Glaciar": "#a8dadc", "Nevado": "#e0fbfc", "CentroPoblado": "#e76f51", "Evento": "#d62828",
           "Cordillera": "#6c757d", "Cuenca": "#52b788", "Quebrada": "#74c69d", "Rio": "#48cae4", "Bofedal": "#2a9d8f",
           "Departamento": "#495057", "Provincia": "#868e96", "Distrito": "#adb5bd", "Institucion": "#f4a261",
           "MetodoMonitoreo": "#9b5de5", "ObraSeguridad": "#f77f00", "NivelRiesgo": "#9d0208", "EstadoObra": "#bc6c25",
           "Fenomeno": "#bc6c25", "ServicioEcosistemico": "#52b788", "Especie": "#95d5b2", "Ecosistema": "#74c69d",
           "Medicion": "#90be6d", "Inventario": "#43aa8b"}


def nombres_en(r, maximo=30):
    """Nombres de entidades que aparecen en los hechos usados (para dibujar su subgrafo)."""
    ks = []
    for f in r["fuentes"]:
        if f["tipo"] != "grafo":
            continue
        for parte in re.split(r" · |—\w+→|: |, ", f["texto"]):
            k = norm(re.sub(r"\(.*?\)|^paso \d+", "", parte, flags=re.I))
            if len(k) >= 4 and k not in ks:
                ks.append(k)
    return ks[:maximo * 3]


def vista_respuesta(r, motor, ruta_html, alto="560px"):
    from pyvis.network import Network
    ks = [k for k in nombres_en(r) if k in motor.enl.por_k or k in set(motor.cat.k)]
    ks += [norm(e.split(" (")[0]) for e in r["plan"].get("entidades", [])]
    ks = list(dict.fromkeys(ks))[:40]
    d = motor.g.aristas_entre(ks) if ks else pd.DataFrame()
    net = Network(height=alto, width="100%", directed=True, cdn_resources="in_line", bgcolor="#ffffff")
    net.barnes_hut(gravity=-4000, spring_length=140)
    vistos = set()
    for x in d.itertuples():
        for nom, tipo in ((x.desde, x.tipo_desde), (x.hacia, x.tipo_hacia)):
            if nom not in vistos:
                net.add_node(nom, label=nom, color=COLORES.get(tipo, "#cccccc"), title=tipo, size=18 if tipo == "Laguna" else 12,
                             font={"size": 16})
                vistos.add(nom)
        net.add_edge(x.desde, x.hacia, label=x.relacion, title=f"{x.relacion} · {x.documento} p. {x.pagina}",
                     font={"size": 10, "align": "middle"}, arrows="to")
    net.write_html(ruta_html, notebook=False)
    return ruta_html, len(vistos), len(d)


# ======================================================================
# 9. Piezas de LangChain para el agente de la semana 11
# ======================================================================
def crear_retriever_grafo(motor):
    """Retriever de LangChain: devuelve los hechos del grafo (con documento y página) como Documents."""
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever

    class RetrieverGrafo(BaseRetriever):
        def _get_relevant_documents(self, query, *, run_manager=None):
            entidades = motor.enl.enlazar(query)
            plan = motor.ruta(query, entidades)
            d = motor.ejecutar(plan, query, entidades)
            H, _ = motor.hechos(d, plan)
            return [Document(page_content=f"{h.texto}" + (f" | evidencia: {h.evidencia}" if h.evidencia else ""),
                             metadata={"paper_id": h.documento, "pagina": h.pagina, "herramienta": plan["herramienta"]})
                    for h in H.itertuples()]

    return RetrieverGrafo()


def herramientas_langchain(motor):
    """Herramientas (tools) para un agente LangGraph: consultar el grafo, buscar en el texto y responder con GraphRAG."""
    from langchain_core.tools import tool

    @tool
    def consultar_grafo(pregunta: str) -> str:
        """Busca en el grafo de conocimiento de glaciares hechos estructurados (listas, conteos, relaciones) con su fuente."""
        entidades = motor.enl.enlazar(pregunta)
        plan = motor.ruta(pregunta, entidades)
        H, res = motor.hechos(motor.ejecutar(plan, pregunta, entidades), plan)
        return json.dumps({"herramienta": plan["herramienta"], "filas": res.get("filas", 0),
                           "hechos": H.head(15).to_dict("records")}, ensure_ascii=False, default=str)

    @tool
    def buscar_texto(pregunta: str) -> str:
        """Busca fragmentos de los 398 documentos (búsqueda híbrida semántica + palabras clave)."""
        T, _ = motor.fragmentos(pregunta, motor.enl.enlazar(pregunta), set())
        return json.dumps(T.head(5).to_dict("records"), ensure_ascii=False, default=str)

    @tool
    def responder_graphrag(pregunta: str) -> str:
        """Responde una pregunta sobre glaciares y lagunas del Perú combinando grafo y texto, con citas."""
        r = motor.responder(pregunta)
        return r["respuesta"] + "\n\nFuentes: " + "; ".join(f"[{f['n']}] {f['referencia']}" for f in r["fuentes"] if f["n"] in r["citadas"])

    return [consultar_grafo, buscar_texto, responder_graphrag]
