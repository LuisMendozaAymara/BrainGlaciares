# -*- coding: utf-8 -*-
"""Saca de los documentos las coordenadas de las lagunas y valida cada una contra el relieve real.

Muchas coordenadas de los informes están mal escritas o las dañó el OCR. Por eso, para cada laguna:
  1. se reúnen todas las coordenadas UTM y geográficas que aparecen junto a su nombre;
  2. se convierten a latitud y longitud (si no dicen la zona UTM, se prueban las zonas 17, 18 y 19);
  3. se compara la altitud del terreno en ese punto con la altitud que dice el documento;
  4. gana el punto con más documentos a favor y mejor altitud.
Solo se publica lo que pasa la validación, con su documento y página.

    python scripts/extraer_coordenadas.py      (necesita Pillow e internet; genera geo/lagunas_geo.csv)
"""
import io
import math
import os
import re
import sys
import urllib.request
from collections import defaultdict

import pandas as pd
from PIL import Image

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)
from graphrag_glaciares import GENERICAS, NO_ENTIDAD, norm  # noqa: E402
from geo import utm_a_latlon  # noqa: E402

DATOS = os.path.join(AQUI, "datos")
SALIDA = os.path.join(AQUI, "geo", "lagunas_geo.csv")
RELIEVE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
Z = 12
TOLERANCIA_M = 150      # diferencia admitida entre el terreno y la altitud del documento
AGRUPAR_KM = 1.5        # dos coordenadas a menos de esta distancia se consideran el mismo punto
VENTANA = 220           # caracteres alrededor del nombre donde se busca la coordenada
NUM = r"\d{1,3}(?:[ ,.]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
PAT_NE = re.compile(rf"\b({NUM})\s*(?:m\.?\s*)?N\b[^\d]{{0,24}}({NUM})\s*(?:m\.?\s*)?E\b")
PAT_EN = re.compile(rf"\b({NUM})\s*(?:m\.?\s*)?E\b[^\d]{{0,24}}({NUM})\s*(?:m\.?\s*)?N\b")
PAT_TABLA = re.compile(r"(?<![\d.,])(\d{6}(?:\.\d+)?)\s*\|?\s*([89]\d{6}(?:\.\d+)?)(?![\d])")
PAT_GRADOS = re.compile(r"(\d{1,2})\s*[°º]\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:[.,]\d+)?)?\s*[\"”″'’]*\s*(?:S\b|de\s+latitud\s+sur|latitud\s+sur|Sur)"
                        r"[^\d]{0,30}(\d{2})\s*[°º]\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:[.,]\d+)?)?")
PAT_ZONA = re.compile(r"(?:zona\s*|UTM\s*[-–]?\s*|WGS\s*84\s*[-–]?\s*)(1[789])\s*[LMKSsl]?\b", re.I)
PAT_ALT = re.compile(r"Altitud[^\d]{0,12}(\d[\d .,]{2,6})", re.I)
RUIDO = {norm(x) for x in """coordenadas caracterización parque futura futuras leche forma región tabla figura cuadro nombre código altitud área volumen
    ubicación descripción general evaluación estudio inventario glaciar cuenca provincia distrito norte este sur oeste karan copap""".split()}
RIESGO_DOCUMENTADO = {"llaca", "pacliashcocha", "taullicocha"}   # con nivel de riesgo en los documentos, aunque Aura no lo conserve


def numero(t):
    t = t.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        t = re.sub(r"[.,]", "", t)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+[.,]\d+", t):
        t = re.sub(r"[.,](?=\d{3}[.,])", "", t).replace(",", ".")
    return float(t.replace(",", "."))


def valido_utm(e, n):
    return 150_000 <= e <= 900_000 and 8_000_000 <= n <= 10_000_000


_tiles = {}


def altitud_terreno(lat, lon):
    """Altitud del terreno según los mosaicos Terrarium (Mapzen, datos públicos), o None sin conexión."""
    n = 2 ** Z
    fx = (lon + 180) / 360 * n
    fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    x, y = int(fx), int(fy)
    if (x, y) not in _tiles:
        try:
            with urllib.request.urlopen(RELIEVE.format(z=Z, x=x, y=y), timeout=30) as r:
                _tiles[(x, y)] = Image.open(io.BytesIO(r.read())).convert("RGB")
        except Exception:
            _tiles[(x, y)] = None
    im = _tiles[(x, y)]
    if im is None:
        return None
    px, py = min(int((fx - x) * 256), 255), min(int((fy - y) * 256), 255)
    r, g, b = im.getpixel((px, py))
    return r * 256 + g + b / 256 - 32768


def km(a, b):
    dy = (a[0] - b[0]) * 111.0
    dx = (a[1] - b[1]) * 111.0 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def en_peru(lat, lon):
    return -18.6 <= lat <= -0.0 and -81.5 <= lon <= -68.5


def coordenadas(texto):
    """[(posición, lat, lon, zona, e, n)] de un fragmento; con zona desconocida se devuelven todas las posibles."""
    salida = []
    zona_txt = PAT_ZONA.search(texto)
    zonas = [int(zona_txt.group(1))] if zona_txt else [18, 19, 17]
    pares = []
    for pat, orden in ((PAT_NE, "ne"), (PAT_EN, "en"), (PAT_TABLA, "en")):
        for m in pat.finditer(texto):
            try:
                a, b = numero(m.group(1)), numero(m.group(2))
            except ValueError:
                continue
            e, n = (b, a) if orden == "ne" else (a, b)
            if valido_utm(e, n):
                pares.append((m.start(), e, n))
    for pos, e, n in pares:
        for z in zonas:
            lat, lon = utm_a_latlon(e, n, z)
            if en_peru(lat, lon):
                salida.append((pos, lat, lon, z, e, n))
    for m in PAT_GRADOS.finditer(texto):
        g = [float((x or "0").replace(",", ".")) for x in m.groups()]
        lat, lon = -(g[0] + g[1] / 60 + g[2] / 3600), -(g[3] + g[4] / 60 + g[5] / 3600)
        if en_peru(lat, lon):
            salida.append((m.start(), lat, lon, None, None, None))
    return salida


def main():
    N = pd.read_csv(os.path.join(DATOS, "nodos.csv"), dtype=str, low_memory=False)
    F = pd.read_parquet(os.path.join(DATOS, "fragmentos_rag.parquet"), columns=["paper_id", "page_start", "text", "title", "fuente", "year"])
    L = N[(N.tipo == "Laguna") & (N.dudoso.astype(str).str.lower() != "true")]
    L = L[(L.k.str.len() >= 4) & ~L.k.isin(GENERICAS | NO_ENTIDAD)]
    # un nombre que también es departamento, provincia, cordillera, río… no se toma por laguna, salvo que tenga nivel de riesgo
    otros = set(N[N.tipo.isin(["Departamento", "Provincia", "Distrito", "Cordillera", "Rio", "Nevado", "Institucion"])].k.dropna())
    L = L[~L.k.isin(otros) | L.nivel_max.isin(["Muy alto", "Alto"])]
    L = L[~L.k.isin(RUIDO) & ~L.k.str.startswith(("microcuenca", "subcuenca", "rio ", "quebrada "))]
    en_riesgo = set(L[L.nivel_max.isin(["Muy alto", "Alto"])].k) | RIESGO_DOCUMENTADO
    nombres = {k: g.iloc[0] for k, g in L.groupby("k")}
    fuertes = set(L[L.k.str.contains(r"cocha|cucho|pampa$", regex=True) | L.nivel_max.isin(["Muy alto", "Alto"])
                    | (pd.to_numeric(L.n_docs, errors="coerce") >= 5)].k)
    alt_doc = defaultdict(list)
    R = pd.read_csv(os.path.join(DATOS, "relaciones.csv"), dtype=str, low_memory=False)
    M = N[(N.tipo == "Medicion") & (N.propiedad == "tieneAltitud")].set_index("id")
    for r in R[(R.pred == "tieneAltitud") & R.o.isin(M.index)].itertuples():
        v = pd.to_numeric(M.at[r.o, "valor"], errors="coerce") if r.o in M.index else None
        if v == v and v and 2500 < float(v) < 6800:
            alt_doc[r.s.split(":", 1)[-1].replace("_", " ")].append(float(v))
    patron = re.compile(r"\b(" + "|".join(sorted(map(re.escape, nombres), key=len, reverse=True)) + r")\b")
    pat_rapido = re.compile(r"[89]\d{6}|\d\s*[°º]\s*\d", re.U)
    candidatos = defaultdict(list)
    revisados = 0
    for f in F[F.text.str.contains(pat_rapido)].itertuples():
        texto = str(f.text)
        cs = coordenadas(texto)
        if not cs:
            continue
        revisados += 1
        t_norm = "".join(c if len(norm(c)) == 1 else c for c in norm(texto))
        # el grafo incluye ruido etiquetado como laguna ("forma", "region"…): vale el nombre escrito como "laguna X" o uno conocido
        explicitas = set(re.findall(r"\b(?:laguna|lagunas|lag\.)\s+(?:de\s+)?(" + patron.pattern[3:-3] + r")\b", t_norm))
        # si el texto escribe "laguna X", esa manda sobre cualquier otro nombre cercano ("Cordillera: Blanca" le quitaba el dato a Parón)
        validas = explicitas or fuertes
        menciones = [(m.start(), m.group(1)) for m in patron.finditer(t_norm) if m.group(1) in validas]
        en_titulo = [m.group(1) for m in patron.finditer(norm(str(f.title)))]
        # una ficha describe una laguna: el nombre puede estar lejos de "Coordenadas: UTM…", o solo en el título del informe
        principal = None
        if re.search(r"coordenadas|ubicaci[oó]n|descripci[oó]n general", texto, re.I):
            conteo = pd.Series([k for _, k in menciones]).value_counts() if menciones else pd.Series(dtype=int)
            if len(conteo) and (len(conteo) == 1 or conteo.iloc[0] > conteo.iloc[1] or explicitas):
                principal = conteo.index[0]
            elif not len(conteo) and len(set(en_titulo)) == 1:
                principal = en_titulo[0]
        if not menciones and principal is None:
            continue
        for pos, lat, lon, z, e, n in cs:
            # en las tablas el nombre va antes de la coordenada: el nombre siguiente es de otra fila
            cerca = [((pos - p) if p <= pos else 3 * (p - pos), k) for p, k in menciones if abs(p - pos) <= VENTANA]
            if not cerca and principal is None:
                continue
            if not cerca:
                cerca = [(VENTANA, principal)]
            k = min(cerca)[1]
            m_alt = PAT_ALT.search(texto[max(0, pos - 40):pos + 200])
            alt = None
            if m_alt:
                try:
                    a = numero(m_alt.group(1).strip(" .,"))
                    alt = a * 1000 if a < 7 else a
                except ValueError:
                    pass
            base = {"lat": lat, "lon": lon, "zona": z, "este": e, "norte": n, "alt": alt, "doc": f.paper_id,
                    "pag": None if pd.isna(f.page_start) else int(f.page_start), "fuente": f.fuente, "anio": f.year}
            candidatos[k].append({**base, "dist": min(cerca)[0]})
            # las lagunas en riesgo son las que más importan: se prueba también con otras cercanas; el relieve decide
            for p, k2 in menciones:
                if k2 != k and k2 in en_riesgo and abs(p - pos) <= 2 * VENTANA:
                    candidatos[k2].append({**base, "alt": None, "dist": abs(p - pos)})
    print(f"Fragmentos con coordenadas: {revisados} · lagunas con candidatos: {len(candidatos)}", flush=True)
    filas, rechazadas = [], 0
    for k, cs in candidatos.items():
        grupos = []
        for c in sorted(cs, key=lambda x: x["dist"]):
            for g in grupos:
                if km((c["lat"], c["lon"]), (g[0]["lat"], g[0]["lon"])) <= AGRUPAR_KM:
                    g.append(c)
                    break
            else:
                grupos.append([c])
        mejor, puntos = None, -1e9
        for g in grupos[:60]:
            c = g[0]
            terreno = altitud_terreno(c["lat"], c["lon"])
            if terreno is None:
                continue
            referencias = [x["alt"] for x in g if x["alt"]] + alt_doc.get(k, [])
            if referencias:
                ref = float(pd.Series(referencias).median())
                dif = abs(terreno - ref)
                if dif > TOLERANCIA_M:
                    continue
            else:
                ref, dif = None, None
                if terreno < 3200:    # sin altitud en el documento: una laguna glaciar no está por debajo de esta cota
                    continue
            docs = {x["doc"] for x in g}
            p = 10 * len(docs) - (dif or 75) / 10 - c["dist"] / 40
            if p > puntos:
                puntos = p
                mejor = (c, g, terreno, ref, dif, docs)
        if mejor is None:
            rechazadas += 1
            continue
        c, g, terreno, ref, dif, docs = mejor
        lat = float(pd.Series([x["lat"] for x in g]).median())
        lon = float(pd.Series([x["lon"] for x in g]).median())
        filas.append({"k": k, "nombre": nombres[k].nombre, "lat": round(lat, 5), "lon": round(lon, 5), "altitud_documento": None if ref is None else round(ref),
                      "altitud_terreno": round(terreno), "diferencia_m": None if dif is None else round(dif), "n_documentos": len(docs),
                      "documento": c["doc"], "pagina": c["pag"], "fuente": c["fuente"], "anio": c["anio"]})
    d = pd.DataFrame(filas)
    # sin altitud que comparar ni un segundo documento, una coordenada automática no basta para publicarla
    d = d[d.altitud_documento.notna() | (d.n_documentos >= 2)].assign(verificada=False, nota="")
    # la curaduría del equipo reemplaza lo automático (mismo criterio que hechos_verificados.csv)
    V = pd.read_csv(os.path.join(AQUI, "geo", "lagunas_verificadas.csv"), dtype={"documento": str})
    curadas = []
    for v in V.itertuples():
        lat, lon = utm_a_latlon(v.este, v.norte, int(v.zona))
        refs = alt_doc.get(v.k, [])
        ref = float(pd.Series(refs).median()) if refs else None
        t = altitud_terreno(lat, lon)
        curadas.append({"k": v.k, "nombre": v.nombre, "lat": round(lat, 5), "lon": round(lon, 5), "altitud_documento": None if ref is None else round(ref),
                        "altitud_terreno": None if t is None else round(t), "diferencia_m": None if ref is None or t is None else round(abs(t - ref)),
                        "n_documentos": 1, "documento": v.documento, "pagina": v.pagina, "fuente": None, "anio": None, "verificada": True, "nota": v.nota})
    d = pd.concat([pd.DataFrame(curadas), d[~d.k.isin(set(V.k))]], ignore_index=True)
    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    d.to_csv(SALIDA, index=False, encoding="utf-8")
    print(f"Publicadas: {len(d)} · verificadas a mano: {int(d.verificada.sum())} · rechazadas por el relieve: {rechazadas}")


if __name__ == "__main__":
    main()
