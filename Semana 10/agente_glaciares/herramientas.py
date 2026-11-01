# -*- coding: utf-8 -*-
"""Las herramientas del agente: envuelven el motor GraphRAG de la semana 10 sin cambiarlo.

Toda evidencia pasa por Contexto.registrar: así los hechos de varias herramientas comparten una sola numeración [n]
y la respuesta final se puede verificar contra lo que realmente se recuperó.
"""
import contextvars
import json
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import List, Optional

import pandas as pd
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

import graphrag_glaciares as GR
from geo import RUIDO_LAGUNAS, Ubicaciones, cifras_nacionales as leer_cifras, riesgo_por_departamento
from grafo_resiliente import _sin_direcciones

METRICAS = {"volumen": ("tieneVolumen", "m³"), "profundidad": ("tieneProfundidadMax", "m")}
# El área del grafo mezcla cuencas con espejos de agua (Palcacocha figura con 249 km²) y la altitud incluye nevados: no sirven para ordenar lagunas.
SIN_RANKING = {"area": "el área del grafo mezcla cuencas y espejos de agua", "altitud": "la altitud del grafo incluye nevados y glaciares"}
RELACIONES_FICHA = ("tieneNivelRiesgo", "amenazaA", "alimentadaPor", "tieneObraSeguridad", "estadoObra", "ocurrioEn", "monitoreadoCon",
                    "tieneVolumen", "tieneAltitud", "ubicadaEn")
NIVELES = {"muy alto": "Muy alto", "alto": "Alto", "medio": "Medio", "bajo": "Bajo"}
ANTESALA = re.compile(r"^(?:(laguna|lagunas|glaciar|nevado|cordillera|subcuenca|microcuenca|cuenca|quebrada|rio|bofedal|ciudad|"
                      r"distrito|provincia|departamento|localidad|poblado|centro poblado)\s+)(?:(?:de|del|de la|la|el)\s+)?")
PROHIBIDO_EXTRA = re.compile(r"(//|/\*|;|\b(?:apoc|dbms|gds|db)\s*\.|\b(?:USE|SHOW|START|STOP|TERMINATE|RENAME|IMPORT|EXPLAIN|PROFILE)\b)", re.I)
_contexto = contextvars.ContextVar("contexto_agente")

# P2 pide un solo nivel: con "las más peligrosas de Cusco" daba 0 filas, porque allí hay riesgo Alto pero no Muy alto
GR.PLANTILLAS["E12_riesgo_departamento"] = dict(
    titulo="Lagunas en riesgo de un departamento, de mayor a menor riesgo",
    params={"niveles": GR.EN_RIESGO, "departamento": "Áncash"}, cypher="""
MATCH (l:Laguna)
WHERE l.nivel_max IN $niveles AND $departamento IN l.departamentos
OPTIONAL MATCH (l)-[r:TIENE_NIVEL_RIESGO]->(n:NivelRiesgo)
WHERE n.nombre = l.nivel_max
RETURN l.nombre AS laguna, l.nivel_max AS nivel, r.documento AS documento, r.pagina AS pagina, r.evidencia AS evidencia
ORDER BY nivel DESC, laguna""")
GR.ROTULOS["E12_riesgo_departamento"] = "lagunas en riesgo o más peligrosas de un departamento, de mayor a menor riesgo (parámetro: departamento)"


def validar_cypher_estricto(c, limite=50):
    """La validación de la semana 10 más: sin comentarios, sin varias sentencias y sin procedimientos del sistema."""
    c = GR.validar_cypher(c, limite)
    sin_cadenas = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", c)
    if PROHIBIDO_EXTRA.search(sin_cadenas):
        raise ValueError("la consulta usa una instrucción no permitida")
    return c


class MotorAgente(GR.GraphRAG):
    def text2cypher(self, pregunta, plan):
        ejemplos = "\n\n".join(f"Pregunta: {GR.PLANTILLAS[k]['titulo']}\n{GR.PLANTILLAS[k]['cypher'].strip()}"
                               for k in ("P2", "P6", "E3_conteo_departamento"))
        sistema = GR.INSTRUCCIONES_CYPHER.format(esquema=self.esquema, limite=self.cfg["LIMIT_CYPHER"], ejemplos=ejemplos)
        msg = [("system", sistema), ("human", pregunta)]
        ultimo_error, previa = None, ""
        for intento in range(2):
            if ultimo_error:
                msg = msg[:2] + [("ai", previa), ("human", f"Esa consulta falló: {ultimo_error}. Corrígela y responde solo con la consulta.")]
            try:
                previa = self._llm(msg)
                c = validar_cypher_estricto(previa, self.cfg["LIMIT_CYPHER"])
                plan.update(cypher=c, parametros={}, consulta="Text2Cypher (consulta escrita por el LLM y validada)")
                d = self.g.q(c)
                plan["intentos_cypher"] = intento + 1
                return d
            except Exception as e:
                ultimo_error = _sin_direcciones(str(e))[:300]
        plan["error_cypher"] = ultimo_error
        return pd.DataFrame()


@dataclass
class Contexto:
    """Lo que el agente va reuniendo mientras responde una pregunta."""
    pregunta: str
    fuentes: list = field(default_factory=list)
    bloques: list = field(default_factory=list)
    pasos: list = field(default_factory=list)
    visuales: list = field(default_factory=list)   # mapas, gráficos y fichas que la página dibuja junto a la respuesta
    llamadas: dict = field(default_factory=dict)
    _vistos: dict = field(default_factory=dict)

    def registrar(self, tipo, texto, referencia, documento=None, pagina=None, evidencia=""):
        """Numera una evidencia; si ya se había recuperado, devuelve su número anterior."""
        clave = (tipo, str(texto)[:300], str(documento), str(pagina))
        if clave in self._vistos:
            return self._vistos[clave]
        n = len(self.fuentes) + 1
        documento = None if GR._vacio(documento) else str(documento)
        pagina = None if GR._vacio(pagina) else GR._fmt(pagina)
        self.fuentes.append({"n": n, "tipo": tipo, "texto": texto, "referencia": referencia, "documento": documento,
                             "pagina": pagina, "evidencia": evidencia or ""})
        if tipo == "grafo":
            b = f"[{n}] (grafo) {texto}" + (f"\n    evidencia: \"{evidencia}\"" if evidencia else "") + f"\n    fuente: {referencia}"
        else:
            b = f"[{n}] (texto) {referencia}\n{texto}"
        self.bloques.append(b)
        self._vistos[clave] = n
        return n

    def documentos_grafo(self):
        return {f["documento"] for f in self.fuentes if f["tipo"] == "grafo" and f["documento"]}


# ======================================================================
# Argumentos de cada herramienta (el LLM ve estas descripciones)
# ======================================================================
class ArgsBuscarEntidad(BaseModel):
    nombre: str = Field(description="Nombre aproximado de la entidad, por ejemplo 'Palcacoha' o 'nevado Huascaran'.")


class ArgsConsulta(BaseModel):
    consulta_id: str = Field(description="Identificador de la consulta verificada, por ejemplo 'P2' o 'E3_conteo_departamento'.")
    departamento: Optional[str] = Field(default=None, description="Departamento del Perú, por ejemplo 'Cusco'.")
    nivel: Optional[str] = Field(default=None, description="Nivel de riesgo: 'Muy alto', 'Alto', 'Medio' o 'Bajo'.")
    niveles: Optional[List[str]] = Field(default=None, description="Lista de niveles de riesgo, por ejemplo ['Muy alto'].")
    cordillera: Optional[str] = Field(default=None, description="Cordillera sin la palabra 'cordillera', por ejemplo 'blanca'.")
    anio: Optional[int] = Field(default=None, description="Año del inventario.")
    hasta: Optional[int] = Field(default=None, description="Año límite para la línea de tiempo de eventos.")


class ArgsFicha(BaseModel):
    nombres: List[str] = Field(description="Una a tres entidades nombradas, por ejemplo ['Palcacocha'].")
    relaciones: Optional[List[str]] = Field(default=None, description=(
        "Solo estas relaciones, si la pregunta pide algo concreto: amenazaA, alimentadaPor, tieneNivelRiesgo, tieneObraSeguridad, "
        "estadoObra, monitoreadoCon, ubicadaEn, presentaFenomeno, ocurrioEn, afectoA, intervieneEn, tieneVolumen, tieneArea, tieneAltitud."))


class ArgsCamino(BaseModel):
    a: str = Field(description="Primera entidad.")
    b: str = Field(description="Segunda entidad.")


class ArgsConsultaLibre(BaseModel):
    pedido: str = Field(description="Lo que debe devolver la consulta, en lenguaje natural y con todos sus filtros.")


class ArgsDocumentos(BaseModel):
    consulta: str = Field(description="Qué buscar; incluye los nombres propios y términos técnicos de la pregunta.")
    fuente: Optional[str] = Field(default=None, description="Solo documentos de esta institución: INAIGEM, ANA o SENAMHI.")
    anio_min: Optional[int] = Field(default=None, description="Solo documentos publicados desde este año.")
    anio_max: Optional[int] = Field(default=None, description="Solo documentos publicados hasta este año.")


class ArgsCifras(BaseModel):
    cordillera: Optional[str] = Field(default=None, description="Para ver además el inventario de una cordillera, por ejemplo 'blanca'.")


class ArgsRanking(BaseModel):
    metrica: str = Field(description="volumen (para las más grandes) o profundidad.")
    top: int = Field(default=20, description="Cuántas lagunas pidió la persona (máximo 30).")
    departamento: Optional[str] = Field(default=None, description="Solo este departamento.")


class ArgsNombre(BaseModel):
    nombre: str = Field(description="Nombre de la laguna o entidad, por ejemplo 'Palcacocha'.")


class ArgsMapa(BaseModel):
    nombres: Optional[List[str]] = Field(default=None, description="Lagunas concretas. Sin esto, todas las que cumplan los filtros.")
    departamento: Optional[str] = Field(default=None, description="Solo este departamento.")
    solo_en_riesgo: bool = Field(default=False, description="Solo lagunas con riesgo alto o muy alto en el grafo.")
    titulo: Optional[str] = Field(default=None, description="Título breve del mapa.")


class ArgsGrafico(BaseModel):
    tema: str = Field(description="superficie_cordillera, lagunas_por_cordillera, riesgo_por_departamento o eventos_por_decada.")
    cordillera: Optional[str] = Field(default=None, description="Para superficie_cordillera, por ejemplo 'blanca'.")


class ArgsSuperficie(BaseModel):
    cordillera: str = Field(description="Cordillera sin la palabra 'cordillera', por ejemplo 'blanca'.")
    anio_1: int = Field(default=2018, description="Año inicial.")
    anio_2: int = Field(default=2023, description="Año final.")


# ======================================================================
# Herramientas
# ======================================================================
class CajaHerramientas:
    def __init__(self, motor, geo=None):
        self.m = motor
        self.geo = geo or Ubicaciones()
        self.cifras = leer_cifras()
        self.riesgo = riesgo_por_departamento()
        self._nombres_k = list(motor.enl.por_k)
        catalogo = "\n".join(f"- {k}: {v}" for k, v in GR.ROTULOS.items())
        especiales = [
            ("buscar_entidad", self.buscar_entidad, ArgsBuscarEntidad,
             "Busca en el catálogo del grafo una entidad por su nombre aproximado. Úsala si no sabes cómo está escrito un nombre "
             "o si otra herramienta no lo reconoció. No devuelve evidencia citable."),
            ("consulta_verificada", self.consulta_verificada, ArgsConsulta,
             "Ejecuta una consulta Cypher ya probada sobre todo el grafo. Es la primera opción para listas, conteos, negaciones y "
             "cadenas de varios saltos, si coincide con los filtros de la pregunta. Consultas disponibles:\n" + catalogo),
            ("ficha_entidad", self.ficha_entidad, ArgsFicha,
             "Hechos sueltos de entidades concretas (glaciares, poblados, lagunas), con documento y página. Si piden una ficha o informe de una laguna, usa ficha_laguna."),
            ("camino_entidades", self.camino_entidades, ArgsCamino,
             "Encuentra la cadena de relaciones más corta que conecta dos entidades. Úsala cuando preguntan cómo se relacionan."),
            ("consulta_libre", self.consulta_libre, ArgsConsultaLibre,
             "Escribe y ejecuta una consulta Cypher nueva, validada y de solo lectura. Úsala solo si ninguna consulta verificada "
             "cubre los filtros de la pregunta."),
            ("buscar_documentos", self.buscar_documentos, ArgsDocumentos,
             "Busca fragmentos en los documentos del corpus (semántica y palabras clave). Úsala para explicar causas, "
             "contexto, definiciones o hechos históricos, y para complementar los hechos del grafo."),
            ("comparar_superficie", self.comparar_superficie, ArgsSuperficie,
             "Calcula el cambio de la superficie glaciar de una cordillera entre dos inventarios."),
            ("cifras_nacionales", self.cifras_nacionales, ArgsCifras,
             "Totales oficiales del Perú con su fuente: cuántas lagunas y glaciares hay, cuántas lagunas tienen riesgo, cuál es la de mayor "
             "riesgo. Úsala SIEMPRE para '¿cuántas hay en el Perú?'; contar nodos del grafo no da el total real."),
            ("ranking_lagunas", self.ranking_lagunas, ArgsRanking,
             "Las lagunas más grandes (por volumen) o más profundas registradas, con su fuente; también dibuja el gráfico."),
            ("ficha_laguna", self.ficha_laguna, ArgsNombre,
             "Informe de riesgo de una laguna: nivel, obras y su estado, poblados, glaciares, eventos, monitoreo y medidas. Úsala para "
             "'háblame de', 'situación de' o 'qué se sabe de' una laguna."),
            ("mostrar_mapa", self.mostrar_mapa, ArgsMapa,
             "Muestra lagunas en un mapa. Úsala si piden un mapa, dónde está o dónde se ubican."),
            ("vista_3d", self.vista_3d, ArgsNombre,
             "Muestra una laguna sobre el relieve en 3D. Úsala si piden 3D, relieve o terreno."),
            ("graficar", self.graficar, ArgsGrafico,
             "Dibuja un gráfico con datos con fuente. Úsala si piden un gráfico, una comparación o una evolución en el tiempo."),
            ("ver_subgrafo", self.ver_subgrafo, ArgsNombre,
             "Dibuja las conexiones de una entidad en el grafo. Úsala si piden ver sus relaciones o el grafo."),
        ]
        self.herramientas = [StructuredTool.from_function(func=f, name=n, description=d, args_schema=a)
                             for n, f, a, d in especiales]
        self.por_nombre = {t.name: t for t in self.herramientas}

    def ejecutar(self, nombre, args, ctx):
        """Ejecuta una herramienta y registra el paso. Nunca lanza: el error vuelve al LLM para que corrija o cambie de camino."""
        t0 = time.time()
        firma = f"{nombre}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        paso = {"tipo": "herramienta", "herramienta": nombre, "args": args}
        if firma in ctx.llamadas:
            paso.update(repetida=True, ms=0)
            ctx.pasos.append(paso)
            return "Ya ejecutaste esta misma llamada; su resultado sigue vigente:\n" + ctx.llamadas[firma]
        herramienta = self.por_nombre.get(nombre)
        if herramienta is None:
            texto = f"ERROR: no existe la herramienta '{nombre}'. Disponibles: {', '.join(self.por_nombre)}."
            paso["error"] = "herramienta inexistente"
        else:
            fuentes_antes = len(ctx.fuentes)
            token = _contexto.set(ctx)
            try:
                texto = str(herramienta.invoke(args))
                paso["evidencia_nueva"] = len(ctx.fuentes) - fuentes_antes
            except Exception as e:
                detalle = _sin_direcciones(str(e))[:300]
                texto = f"ERROR en {nombre}: {detalle}. Corrige los argumentos o usa otra herramienta."
                paso["error"] = detalle
            finally:
                _contexto.reset(token)
        paso["ms"] = int(1000 * (time.time() - t0))
        ctx.pasos.append(paso)
        ctx.llamadas[firma] = texto
        return texto

    # ---------- utilidades ----------
    def _resolver(self, nombre):
        """Del nombre que escribe el LLM a una fila del catálogo: exacto, sin 'laguna', 'nevado'…, o parecido."""
        k = GR.norm(nombre)
        m = ANTESALA.match(k)
        pista = GR.PISTAS_TIPO.get(m.group(1)) if m else None
        corto = ANTESALA.sub("", k)
        por_k = self.m.enl.por_k
        candidato = next((c for c in (k, corto) if c in por_k), None)
        if candidato is None:
            from rapidfuzz import fuzz, process
            hallado = process.extractOne(corto, self._nombres_k, scorer=fuzz.ratio, score_cutoff=85)
            candidato = hallado[0] if hallado else None
        if candidato is None:
            return None
        g = por_k[candidato]
        if pista and (g.tipo == pista).any():
            g = g[g.tipo == pista]
        return g.iloc[0]

    def _a_evidencia(self, d, plan, encabezado, extra=""):
        ctx = _contexto.get()
        H, resumen = self.m.hechos(d, plan)
        if not len(H):
            return f"{encabezado}: 0 filas. El grafo no registra esto; prueba otra herramienta o dilo en tu respuesta."
        total = resumen.get("filas", len(H))
        lineas = [f"{encabezado}: {total} filas" + (f"; se muestran {len(H)} (dilo en la respuesta)." if total > len(H) else ".")]
        for k_, v in resumen.items():
            if k_.endswith("_distintos"):
                lineas.append(f"Valores distintos de '{k_[:-10]}' en el resultado completo: {v}.")
        if extra:
            lineas.append(extra)
        for r in H.itertuples():
            ref = self.m._ref_doc(r.documento, r.pagina) if not GR._vacio(r.documento) \
                else f"Grafo de conocimiento · {plan.get('consulta', plan['herramienta'])}"
            n = ctx.registrar("grafo", r.texto, ref, r.documento, r.pagina, r.evidencia)
            lineas.append(ctx.bloques[n - 1])
        return "\n".join(lineas)

    # ---------- las herramientas ----------
    def buscar_entidad(self, nombre):
        from rapidfuzz import fuzz, process
        corto = ANTESALA.sub("", GR.norm(nombre))
        hallados = process.extract(corto, self._nombres_k, scorer=fuzz.WRatio, limit=5, score_cutoff=70)
        if not hallados:
            return f"No hay ninguna entidad parecida a '{nombre}' en el grafo."
        filas = []
        for k, puntos, _ in hallados:
            g = self.m.enl.por_k[k].iloc[0]
            f = {"nombre": g.nombre, "tipo": g.tipo, "documentos": int(g.n_docs or 0), "parecido": round(puntos)}
            for extra in ("nivel_max", "departamentos"):
                v = g.get(extra)
                if not GR._vacio(v):
                    f[extra] = v if isinstance(v, str) else list(v)
            filas.append(f)
        return json.dumps(filas, ensure_ascii=False, default=str)

    def consulta_verificada(self, consulta_id, departamento=None, nivel=None, niveles=None, cordillera=None, anio=None, hasta=None):
        if consulta_id not in GR.PLANTILLAS:
            raise ValueError(f"'{consulta_id}' no existe. Opciones: {', '.join(GR.PLANTILLAS)}")
        if not self.m.g.cypher_ok:
            raise RuntimeError("las consultas no están disponibles ahora; usa ficha_entidad o buscar_documentos")
        ctx = _contexto.get()
        params = {}
        if departamento:
            dep = GR.DEPARTAMENTOS_N.get(GR.norm(departamento))
            if dep is None:
                raise ValueError(f"'{departamento}' no es un departamento del Perú")
            params["departamento"] = dep
        if nivel:
            params["nivel"] = NIVELES.get(GR.norm(nivel), nivel)
        if niveles:
            params["niveles"] = [NIVELES.get(GR.norm(x), x) for x in niveles]
        for k_, v in (("cordillera", cordillera), ("anio", anio), ("hasta", hasta)):
            if v not in (None, ""):
                params[k_] = v
        plan = {"herramienta": consulta_id, "parametros_llm": params}
        entidades = self.m.enl.enlazar(ctx.pregunta)
        d = self.m.ejecutar(plan, ctx.pregunta, entidades)
        extra = ""
        if plan.get("resumen_calculado"):
            extra = "RESUMEN CALCULADO: " + json.dumps(plan["resumen_calculado"], ensure_ascii=False, default=str)
        return self._a_evidencia(d, plan, f"{consulta_id} ({plan.get('consulta')})", extra)

    def ficha_entidad(self, nombres, relaciones=None):
        filas = [(n, self._resolver(n)) for n in nombres[:3]]
        no_halladas = [n for n, f in filas if f is None]
        ids = [f.id for _, f in filas if f is not None]
        if not ids:
            return f"No encontré en el grafo: {', '.join(no_halladas)}. Usa buscar_entidad para ver nombres parecidos."
        limite = 300 if relaciones else self.m.cfg["MAX_HECHOS_FICHA"]
        d = self.m.g.ficha(ids, limite)
        if relaciones and len(d):
            pedidas = {GR.norm(r) for r in relaciones}
            filtrado = d[d.relacion.map(GR.norm).isin(pedidas)]
            if not len(filtrado):   # sin esto, el LLM repite la misma llamada cambiando poco o nada
                return (f"0 hechos con las relaciones {', '.join(relaciones)}. "
                        f"Relaciones que sí tiene esta entidad: {', '.join(sorted(set(d.relacion.dropna())))}.")
            d = filtrado
        plan = {"herramienta": "ficha"}
        aviso = f"No se encontró: {', '.join(no_halladas)}." if no_halladas else ""
        nombres_ok = ", ".join(f.nombre for _, f in filas if f is not None)
        return self._a_evidencia(d, plan, f"Ficha de {nombres_ok}", aviso)

    def camino_entidades(self, a, b):
        fa, fb = self._resolver(a), self._resolver(b)
        faltan = [n for n, f in ((a, fa), (b, fb)) if f is None]
        if faltan:
            return f"No encontré en el grafo: {', '.join(faltan)}. Usa buscar_entidad para ver nombres parecidos."
        if fa.id == fb.id:
            return f"'{a}' y '{b}' son la misma entidad del grafo ({fa.nombre})."
        plan = {"herramienta": "camino"}
        d = self.m.g.camino(fa.id, fb.id)
        if d is None or not len(d):
            return f"El grafo no conecta {fa.nombre} con {fb.nombre} en 5 saltos o menos."
        return self._a_evidencia(d, plan, f"Camino de {fa.nombre} a {fb.nombre}")

    def consulta_libre(self, pedido):
        g = self.m.g
        if self.m.llm is None or not (g.cypher_ok and g.cypher_libre_ok):
            raise RuntimeError("las consultas libres solo están disponibles con Neo4j; usa consulta_verificada o ficha_entidad")
        plan = {"herramienta": "cypher"}
        d = self.m.text2cypher(pedido, plan)
        if plan.get("error_cypher"):
            raise ValueError(f"no se pudo escribir una consulta válida: {plan['error_cypher']}")
        ctx = _contexto.get()
        ctx.pasos.append({"tipo": "cypher", "cypher": plan.get("cypher"), "intentos": plan.get("intentos_cypher")})
        aviso = ""
        if re.search(r"\bcount\s*\(", plan.get("cypher") or "", re.I):
            aviso = ("AVISO: este conteo solo refleja lo que mencionan los documentos en el grafo, no el total real. "
                     "No lo presentes como cuántas existen; para totales del Perú usa cifras_nacionales.")
        return self._a_evidencia(d, plan, "Consulta nueva (validada, solo lectura)", aviso)

    def buscar_documentos(self, consulta, fuente=None, anio_min=None, anio_max=None):
        indice = self.m.indice
        if indice is None:
            raise RuntimeError("la búsqueda en documentos no está disponible")
        ctx = _contexto.get()
        filtros = {k_: v for k_, v in (("fuente", [fuente] if fuente else None), ("anio_min", anio_min), ("anio_max", anio_max)) if v}

        class _Filtrado:
            cfg = indice.cfg

            @staticmethod
            def buscar(q, k=20, modo="hibrido"):
                return indice.buscar(q, k=k, modo=modo, filtros=filtros or None)

        motor_breve = SimpleNamespace(indice=_Filtrado, cfg=self.m.cfg, R=self.m.R)
        T, sim_max = GR.GraphRAG.fragmentos(motor_breve, consulta, self.m.enl.enlazar(consulta), ctx.documentos_grafo())
        umbral = indice.cfg.get("UMBRAL_SIM")
        if not len(T) or (sim_max is not None and umbral is not None and sim_max == sim_max and sim_max < umbral):
            return "No hay fragmentos relevantes en el corpus para esa búsqueda."
        lineas = [f"{len(T)} fragmentos:"]
        for r in T.itertuples():
            n = ctx.registrar("texto", r.texto, r.referencia, r.paper_id or None)
            lineas.append(ctx.bloques[n - 1])
        return "\n".join(lineas)

    def comparar_superficie(self, cordillera, anio_1=2018, anio_2=2023):
        if not self.m.g.cypher_ok:
            raise RuntimeError("las consultas no están disponibles ahora; usa buscar_documentos")
        ctx = _contexto.get()
        k = GR.norm(cordillera).replace("cordillera ", "")
        d = self.m.g.q(GR.CONSULTAS["P9"]["cypher"], cordillera=k)
        r = GR.comparar_superficie(d, anio_1, anio_2)
        if not len(r):
            return f"El grafo no tiene superficies glaciares de la cordillera '{cordillera}'."
        x = r.iloc[0]
        texto = (f"Superficie glaciar de la cordillera {cordillera}: {x.anio_1}: {x.km2_1} km² → {x.anio_2}: {x.km2_2} km². "
                 f"Cambio calculado: {x.cambio_km2} km² ({x['cambio_%']} %)." + (f" Nota: {x.nota}" if x.nota else ""))
        doc = str(x.fuente_1).split(" p.")[0]
        self.m.titulos([doc])
        referencia = f"{x.fuente_1} y {x.fuente_2}"
        n = ctx.registrar("grafo", texto, referencia, doc)
        return ctx.bloques[n - 1]

    # ---------- datos oficiales y rankings ----------
    def cifras_nacionales(self, cordillera=None):
        ctx = _contexto.get()
        self.m.titulos({c["documento"] for c in self.cifras})
        lineas = ["Cifras oficiales de los documentos del INAIGEM:"]
        for c in self.cifras:
            ref = self.m._ref_doc(c["documento"], c["pagina"])
            n = ctx.registrar("grafo", f"{c['indicador']}: {c['texto']}", ref, c["documento"], c["pagina"], c["texto"])
            lineas.append(ctx.bloques[n - 1])
        if cordillera and self.m.g.cypher_ok:
            k = GR.norm(cordillera).replace("cordillera ", "")
            d = self.m.g.q("""MATCH (i:Inventario)-[:INVENTARIO_DE]->(c:Cordillera {k: $cordillera})
MATCH (i)-[:TIENE_MEDICION]->(m:Medicion)
WHERE m.propiedad IN ['registraLagunas', 'registraLagunasNuevas', 'registraGlaciares', 'registraSuperficieGlaciar']
RETURN i.anio AS anio, m.propiedad AS dato, m.texto AS valor, m.documento AS documento, m.pagina AS pagina, m.evidencia AS evidencia
ORDER BY anio DESC""", cordillera=k)
            if len(d):
                d = d[d.anio == d.anio.max()]
                lineas.append(self._a_evidencia(d, {"herramienta": "cifras"}, f"Inventario de la cordillera {cordillera}"))
        return "\n".join(lineas)

    def ranking_lagunas(self, metrica, top=20, departamento=None):
        if metrica in SIN_RANKING:
            return f"No hay un ranking confiable por {metrica}: {SIN_RANKING[metrica]}. Dilo con claridad y ofrece el ranking por volumen."
        if metrica not in METRICAS:
            raise ValueError(f"métrica desconocida. Opciones: {', '.join(METRICAS)}")
        if not self.m.g.cypher_ok:
            raise RuntimeError("los rankings no están disponibles ahora; usa buscar_documentos")
        ctx = _contexto.get()
        propiedad, unidad = METRICAS[metrica]
        d = self.m.g.q("""MATCH (l:Laguna)-[:TIENE_MEDICION]->(m:Medicion)
WHERE m.propiedad = $propiedad AND m.valor IS NOT NULL
RETURN l.nombre AS laguna, l.k AS k, l.departamentos AS departamentos, m.valor AS valor, m.texto AS texto,
       m.documento AS documento, m.pagina AS pagina, m.evidencia AS evidencia""", propiedad=propiedad)
        d = d[d.k.map(nombre_de_laguna_valido) & d.k.isin(self.m.enl.por_k)]
        if departamento:
            dep = GR.DEPARTAMENTOS_N.get(GR.norm(departamento))
            if dep is None:
                raise ValueError(f"'{departamento}' no es un departamento del Perú")
            dep = "Áncash" if GR.norm(dep) == "ancash" else dep
            d = d[d.departamentos.map(lambda v: dep in _lista(v))]
        if not len(d):
            return f"El grafo no registra {metrica} de lagunas con esos filtros."
        d = d.assign(valor=[valor_corregido(v, t, metrica) for v, t in zip(d.valor, d.texto)])
        d = d.sort_values("valor", ascending=False).drop_duplicates("k")
        pedidas = max(1, min(int(top), 30))
        mejores = d.head(pedidas)
        self.m.titulos(set(mejores.documento.dropna()))
        lineas = [f"Ranking de {len(mejores)} lagunas por {metrica}, con los valores que registran los documentos"
                  + (f"; solo hay {len(mejores)} de las {pedidas} pedidas: dilo." if len(mejores) < pedidas else ".")
                  + " No es un ranking oficial: indícalo y enumera todas."]
        for r in mejores.itertuples():
            texto = f"{r.laguna}: {_cifra(r.valor)} {unidad}"
            n = ctx.registrar("grafo", texto, self.m._ref_doc(r.documento, r.pagina), r.documento, r.pagina)
            lineas.append(f"[{n}] {texto} · {self.m._ref_doc(r.documento, r.pagina)}")
        ctx.visuales.append({"tipo": "barras", "titulo": f"Lagunas con mayor {metrica}" + (f" en {departamento}" if departamento else ""), "unidad": unidad,
                             "etiquetas": mejores.laguna.head(15).tolist(), "valores": [float(v) for v in mejores.valor.head(15)],
                             "fuente": "Valores registrados en los documentos; no es un ranking oficial."})
        return "\n".join(lineas)

    # ---------- ficha de una laguna ----------
    def _nivel_y_departamentos(self, ks):
        if not ks or not self.m.g.cypher_ok:
            return {}
        d = self.m.g.q("MATCH (l:Laguna) WHERE l.k IN $ks RETURN l.k AS k, l.nivel_max AS nivel, l.departamentos AS departamentos", ks=list(ks))
        return {r["k"]: r for r in d.to_dict("records")}

    def _punto(self, u, info=None):
        info = info or {}
        doc = u.get("documento")
        self.m.titulos([doc] if doc else [])
        nivel = info.get("nivel")
        return {"nombre": u["nombre"], "k": u["k"], "lat": float(u["lat"]), "lon": float(u["lon"]), "nivel": None if GR._vacio(nivel) else nivel,
                "departamentos": _lista(info.get("departamentos")),
                "altitud": None if GR._vacio(u.get("altitud_terreno")) else int(u["altitud_terreno"]), "verificada": bool(u.get("verificada")),
                "nota": "" if GR._vacio(u.get("nota")) else str(u["nota"]), "referencia": self.m._ref_doc(doc, u.get("pagina")) if doc else "",
                "url": _url_documento(doc)}

    def ficha_laguna(self, nombre):
        ctx = _contexto.get()
        fila = self._resolver(nombre)
        if fila is None:
            return f"No encontré '{nombre}' en el grafo. Usa buscar_entidad para ver nombres parecidos."
        d = self.m.g.ficha([fila.id], 300)
        if d is None or not len(d):
            return f"El grafo no tiene hechos de {fila.nombre}."
        d = d[d.relacion.isin(RELACIONES_FICHA)].copy()
        d["_orden"] = d.relacion.map({r: i for i, r in enumerate(RELACIONES_FICHA)})
        grupos = {r: list(dict.fromkeys(_medida(c, r) for c in g.con.dropna())) for r, g in d.groupby("relacion")}
        d = d.sort_values(["_orden", "n_docs"], ascending=[True, False]).groupby("relacion", sort=False).head(4).head(20)
        lineas = [self._a_evidencia(d.drop(columns="_orden"), {"herramienta": "ficha"}, f"Ficha de {fila.nombre}")]
        info = self._nivel_y_departamentos([fila.k]).get(fila.k, {})
        u = self.geo.de(fila.nombre)
        ctx.visuales.append({"tipo": "ficha", "nombre": fila.nombre, "entidad": fila.tipo, "nivel": None if GR._vacio(info.get("nivel")) else info["nivel"],
                             "departamentos": _lista(info.get("departamentos")), "obras": grupos.get("tieneObraSeguridad", [])[:8],
                             "estados": grupos.get("estadoObra", [])[:6], "poblados": grupos.get("amenazaA", [])[:10], "glaciares": grupos.get("alimentadaPor", [])[:8],
                             "eventos": grupos.get("ocurrioEn", [])[:8], "monitoreo": grupos.get("monitoreadoCon", [])[:8], "volumen": grupos.get("tieneVolumen", [])[:3],
                             "altitud": grupos.get("tieneAltitud", [])[:2], "punto": self._punto(u, info) if u else None})
        if GR._vacio(info.get("nivel")) and fila.tipo == "Laguna":
            lineas.append("El grafo no registra su nivel de riesgo: búscalo con buscar_documentos antes de afirmar algo.")
        return "\n".join(lineas)

    # ---------- mapa y relieve ----------
    def mostrar_mapa(self, nombres=None, departamento=None, solo_en_riesgo=False, titulo=None):
        ctx = _contexto.get()
        sin_ubicacion = []
        if nombres:
            ubicaciones = []
            for n in nombres[:40]:
                u = self.geo.de(n)
                if u is None:
                    fila = self._resolver(n)
                    u = self.geo.por_k.get(fila.k) if fila is not None else None
                if u:
                    ubicaciones.append(u)
                else:
                    sin_ubicacion.append(n)
        else:
            ubicaciones = list(self.geo.por_k.values())
        info = self._nivel_y_departamentos({u["k"] for u in ubicaciones})
        puntos = [self._punto(u, info.get(u["k"])) for u in ubicaciones]
        if departamento:
            dep = GR.norm(departamento)
            puntos = [p for p in puntos if dep in {GR.norm(x) for x in p["departamentos"]}]
        if solo_en_riesgo:
            puntos = [p for p in puntos if p["nivel"] in ("Muy alto", "Alto")]
        if not puntos:
            return "Ninguna laguna con esos filtros tiene una ubicación verificada en los documentos. " + (f"Sin ubicación: {', '.join(sin_ubicacion)}." if sin_ubicacion else "")
        ctx.visuales.append({"tipo": "mapa", "titulo": titulo or "Ubicación de lagunas", "puntos": puntos, "sin_ubicacion": sin_ubicacion, "tres_d": False})
        extra = f" No tienen ubicación verificada: {', '.join(sin_ubicacion)}; dilo." if sin_ubicacion else ""
        return (f"Mapa mostrado en pantalla con {len(puntos)} lagunas: {', '.join(p['nombre'] for p in puntos[:12])}.{extra} "
                "Las coordenadas vienen de los documentos y se validaron con el relieve. Responde en una o dos frases sin repetir coordenadas.")

    def vista_3d(self, nombre):
        ctx = _contexto.get()
        fila = self._resolver(nombre)
        u = self.geo.de(nombre) or (self.geo.por_k.get(fila.k) if fila is not None else None)
        if u is None:
            return f"'{nombre}' no tiene una ubicación verificada en los documentos, así que no puedo mostrarla en 3D. Dilo con claridad."
        p = self._punto(u, self._nivel_y_departamentos([u["k"]]).get(u["k"]))
        ctx.visuales.append({"tipo": "mapa", "titulo": f"{p['nombre']} sobre el relieve", "puntos": [p], "sin_ubicacion": [], "tres_d": True})
        altitud = f"; el relieve marca {p['altitud']} m s. n. m. en ese punto" if p["altitud"] else ""
        return f"Vista 3D de {p['nombre']} mostrada en pantalla{altitud}. Es el relieve real, no una simulación de desborde: dilo si preguntan por eso. Responde en una o dos frases."

    # ---------- gráficos ----------
    def graficar(self, tema, cordillera=None):
        ctx = _contexto.get()
        if tema == "riesgo_por_departamento" and len(self.riesgo):
            serie = {r.departamento: int(r.lagunas) for r in self.riesgo.itertuples()}
            titulo, unidad, tipo, doc, pag = "Lagunas con riesgo de desborde por departamento", "lagunas", "barras", self.riesgo.documento.iloc[0], int(self.riesgo.pagina.iloc[0])
        elif tema == "lagunas_por_cordillera" and self.m.g.cypher_ok:
            d = self.m.g.q("""MATCH (i:Inventario)-[:TIENE_MEDICION]->(m:Medicion)
WHERE i.anio = 2023 AND m.propiedad = 'registraLagunas' AND m.valor IS NOT NULL
RETURN i.nombre AS inventario, m.valor AS lagunas, m.documento AS documento, m.pagina AS pagina""")
            d = d[~d.inventario.str.contains("Nacional")].sort_values("lagunas", ascending=False).drop_duplicates("inventario")
            serie = {re.sub(r"^Inventario | 2023$", "", r.inventario): float(r.lagunas) for r in d.itertuples()}
            titulo, unidad, tipo, doc, pag = "Lagunas de origen glaciar por cordillera (inventario 2023)", "lagunas", "barras", str(d.documento.iloc[0]), d.pagina.iloc[0]
        elif tema == "superficie_cordillera" and self.m.g.cypher_ok:
            if not cordillera:
                raise ValueError("indica la cordillera")
            k = GR.norm(cordillera).replace("cordillera ", "")
            d = self.m.g.q(GR.CONSULTAS["P9"]["cypher"], cordillera=k).dropna(subset=["anio", "km2"])
            if not len(d):
                return f"El grafo no tiene superficies de la cordillera '{cordillera}'."
            mediana = d.km2.median()
            d = d[(d.km2 >= 0.4 * mediana) & (d.km2 <= 2.5 * mediana)].sort_values(["anio", "confianza"], ascending=[True, False]).drop_duplicates("anio")
            serie = {str(int(r.anio)): float(r.km2) for r in d.itertuples()}
            titulo, unidad, tipo, doc, pag = f"Superficie glaciar de la cordillera {k.title()}", "km²", "linea", str(d.documento.iloc[-1]), d.pagina.iloc[-1]
        elif tema == "eventos_por_decada" and self.m.g.cypher_ok:
            d = self.m.g.q("MATCH (e:Evento) WHERE e.anio IS NOT NULL RETURN e.anio AS anio").dropna()
            d = d[(d.anio >= 1700) & (d.anio <= 2030)]
            decadas = (d.anio.astype(int) // 10 * 10).value_counts().sort_index()
            serie = {f"{a}s": int(v) for a, v in decadas.items()}
            titulo, unidad, tipo, doc, pag = "Eventos registrados en el grafo por década", "eventos", "barras", None, None
        else:
            raise ValueError("tema desconocido o no disponible. Opciones: superficie_cordillera, lagunas_por_cordillera, riesgo_por_departamento, eventos_por_decada")
        if doc:
            self.m.titulos([doc])
        ref = self.m._ref_doc(doc, pag) if doc else "Grafo de conocimiento (eventos mencionados en los documentos)"
        texto = titulo + ": " + "; ".join(f"{a}: {_cifra(v)}" for a, v in serie.items())
        n = ctx.registrar("grafo", texto, ref, doc, pag)
        ctx.visuales.append({"tipo": "linea" if tipo == "linea" else "barras", "titulo": titulo, "unidad": unidad, "etiquetas": list(serie),
                             "valores": [float(v) for v in serie.values()], "fuente": ref})
        return f"Gráfico mostrado en pantalla. Datos:\n{ctx.bloques[n - 1]}\nComenta lo principal en dos o tres frases, citando con [{n}]."

    def ver_subgrafo(self, nombre):
        ctx = _contexto.get()
        fila = self._resolver(nombre)
        if fila is None:
            return f"No encontré '{nombre}' en el grafo. Usa buscar_entidad para ver nombres parecidos."
        d = self.m.g.ficha([fila.id], 60)
        d = d[~d.tipo_con.isin(["Medicion", "Documento"])].sort_values("n_docs", ascending=False).drop_duplicates(["relacion", "con"]).head(24)
        if not len(d):
            return f"El grafo no registra conexiones de {fila.nombre}."
        self.m.titulos(set(d.documento.dropna()))
        aristas = [{"a": r.con, "tipo": r.tipo_con, "relacion": r.relacion, "sale": r.sentido == "sale",
                    "fuente": self.m._ref_doc(r.documento, r.pagina) if not GR._vacio(r.documento) else ""} for r in d.itertuples()]
        ctx.visuales.append({"tipo": "subgrafo", "centro": fila.nombre, "centro_tipo": fila.tipo, "aristas": aristas})
        return self._a_evidencia(d.head(8), {"herramienta": "ficha"}, f"Subgrafo de {fila.nombre} mostrado en pantalla ({len(aristas)} conexiones). Principales")

    # ---------- para la respuesta ----------
    def puntos_en_texto(self, texto):
        """Lagunas con ubicación que la respuesta menciona: la página ofrece verlas en el mapa sin otra llamada al LLM."""
        t = GR.norm(texto)
        presentes = [u for k, u in self.geo.por_k.items() if re.search(rf"\b{re.escape(k)}\b", t)]
        if not presentes:
            return []
        info = self._nivel_y_departamentos({u["k"] for u in presentes})
        return [self._punto(u, info.get(u["k"])) for u in presentes[:30]]


def nombre_de_laguna_valido(k):
    """Descarta el ruido de la extracción que quedó como laguna: "513", "1532 lagunas", "outburst"."""
    k = str(k)
    return len(k) >= 4 and not k[0].isdigit() and "lagunas" not in k and k not in RUIDO_LAGUNAS


def valor_corregido(valor, texto, metrica):
    """"1.269 Hm3" se extrajo como 1269 millones de m³: el decimal se leyó como separador de miles (1000 veces más)."""
    if metrica == "volumen" and valor and valor >= 1e9:
        m = re.match(r"\s*(\d+)[.,](\d{3})\s*(hm|mmc|mm3|millones)", str(texto), re.I)
        if m:
            return (int(m.group(1)) + int(m.group(2)) / 1000) * 1e6
    return valor


def _cifra(v):
    v = float(v)
    return f"{v:,.0f}".replace(",", " ") if abs(v) >= 1000 or v == int(v) else f"{v:.2f}"


def _medida(con, relacion):
    return re.sub(r"^" + re.escape(relacion) + r"\s+", "", str(con))


def _lista(v):
    if isinstance(v, (list, tuple)):
        return list(v)
    return list(v) if hasattr(v, "tolist") else []


def _url_documento(doc):
    return f"https://hdl.handle.net/{doc}" if doc and re.match(r"^\d+(?:\.\d+)+/", str(doc)) else None
