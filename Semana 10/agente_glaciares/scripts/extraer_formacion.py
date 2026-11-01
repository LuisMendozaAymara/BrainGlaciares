# -*- coding: utf-8 -*-
"""Saca la tabla de lagunas glaciares en formación del INAIGEM (documento 20.500.12748/649) y valida cada fila.

Cada fila trae este, norte, cordillera, departamento y altitud. La zona UTM se elige comparando esa altitud con la del
terreno; lo que no coincide se descarta.

    python scripts/extraer_formacion.py      (necesita Pillow e internet; genera geo/lagunas_formacion.csv)
"""
import os
import re
import sys

import pandas as pd

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(AQUI, "scripts"))
from extraer_coordenadas import altitud_terreno  # noqa: E402
from geo import utm_a_latlon  # noqa: E402

DOC = "20.500.12748/649"
DEPTOS = r"ÁNCASH|ANCASH|APURÍMAC|APURIMAC|AREQUIPA|AYACUCHO|CUSCO|HUANCAVELICA|HUÁNUCO|HUANUCO|JUNÍN|JUNIN|LIMA|PASCO|PUNO|MOQUEGUA|TACNA|LA LIBERTAD|CAJAMARCA"
FILA = re.compile(rf"(?<!\d)(\d{{1,3}})\s+(\S{{1,14}}|-)\s+(\d{{6}})\s+(\d{{7}})\s+([A-ZÁÉÍÓÚÑ][\w ÁÉÍÓÚÑáéíóúñ]+?)\s+({DEPTOS})\b(.{{0,160}}?)"
                  r"(\d{1,7}\.\d{2})\s+(\d{4}\.\d{2})\s+\d+\.\d{2}\s+\d+\.\d{2}\s+(Proglaciar|Periglaciar|Supraglaciar)", re.S)
TOLERANCIA_M = 150


def main():
    F = pd.read_parquet(os.path.join(AQUI, "datos", "fragmentos_rag.parquet"), columns=["paper_id", "page_start", "text"])
    filas, vistas, descartadas = [], set(), 0
    for f in F[F.paper_id == DOC].itertuples():
        for m in FILA.finditer(" ".join(str(f.text).split())):
            este, norte, cordillera, depto, area, altitud, clase = int(m[3]), int(m[4]), m[5].strip(), m[6], float(m[8]), float(m[9]), m[10]
            if (este, norte) in vistas:
                continue
            vistas.add((este, norte))
            mejor = None
            for zona in (18, 19, 17):
                lat, lon = utm_a_latlon(este, norte, zona)
                t = altitud_terreno(lat, lon)
                if t is not None and abs(t - altitud) <= TOLERANCIA_M and (mejor is None or abs(t - altitud) < mejor[0]):
                    mejor = (abs(t - altitud), lat, lon)
            if mejor is None:
                descartadas += 1
                continue
            filas.append({"lat": round(mejor[1], 5), "lon": round(mejor[2], 5), "altitud": round(altitud), "area_m2": area, "tipo": clase,
                          "cordillera": cordillera, "departamento": "Áncash" if depto.upper() in ("ANCASH", "ÁNCASH") else depto.title(), "documento": DOC, "pagina": int(f.page_start)})
    d = pd.DataFrame(filas)
    d.to_csv(os.path.join(AQUI, "geo", "lagunas_formacion.csv"), index=False, encoding="utf-8")
    print(f"Publicadas: {len(d)} · descartadas por el relieve: {descartadas}")
    if len(d):
        print(d.departamento.value_counts().to_dict(), d.tipo.value_counts().to_dict())


if __name__ == "__main__":
    main()
