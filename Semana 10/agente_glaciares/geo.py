# -*- coding: utf-8 -*-
"""Ubicación de las lagunas: coordenadas sacadas de los documentos y validadas con el relieve (scripts/extraer_coordenadas.py)."""
import math
import os

import pandas as pd

from graphrag_glaciares import norm

RUTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo", "lagunas_geo.csv")


def _tabla(archivo, **opciones):
    ruta = os.path.join(os.path.dirname(RUTA), archivo)
    return pd.read_csv(ruta, **opciones) if os.path.exists(ruta) else pd.DataFrame()


# palabras que la extracción etiquetó como laguna en la copia del grafo
RUIDO_LAGUNAS = {norm(x) for x in _tabla("ruido_lagunas.csv").get("palabra", [])}


def riesgo_por_departamento():
    return _tabla("riesgo_departamento.csv", dtype={"documento": str})


def utm_a_latlon(este, norte, zona):
    """UTM del hemisferio sur (WGS84) a latitud y longitud, sin dependencias externas."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    x, y = este - 500000.0, norte - 10000000.0
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    fi = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
          + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    s, c, t = math.sin(fi), math.cos(fi), math.tan(fi)
    n1 = a / math.sqrt(1 - e2 * s ** 2)
    t1, c1 = t ** 2, ep2 * c ** 2
    r1 = a * (1 - e2) / (1 - e2 * s ** 2) ** 1.5
    d = x / (n1 * k0)
    lat = fi - (n1 * t / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
                                + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / c
    return math.degrees(lat), (zona - 1) * 6 - 180 + 3 + math.degrees(lon)


def cargar(ruta=RUTA):
    if not os.path.exists(ruta):
        return pd.DataFrame(columns=["k", "nombre", "lat", "lon", "documento", "pagina"])
    return pd.read_csv(ruta, dtype={"documento": str})


def cifras_nacionales(ruta=None):
    ruta = ruta or os.path.join(os.path.dirname(RUTA), "cifras_nacionales.csv")
    if not os.path.exists(ruta):
        return []
    return pd.read_csv(ruta, dtype={"documento": str}).to_dict("records")


def lagunas_formacion(ruta=None):
    ruta = ruta or os.path.join(os.path.dirname(RUTA), "lagunas_formacion.csv")
    if not os.path.exists(ruta):
        return []
    return pd.read_csv(ruta, dtype={"documento": str}).to_dict("records")


class Ubicaciones:
    def __init__(self, ruta=RUTA):
        self.d = cargar(ruta)
        self.por_k = {r["k"]: r for r in self.d.to_dict("records")}

    def de(self, nombre):
        k = norm(nombre)
        if k.startswith("laguna "):
            k = k[7:]
        return self.por_k.get(k)

    def __len__(self):
        return len(self.d)
