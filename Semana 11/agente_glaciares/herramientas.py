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
from grafo_resiliente import _sin_direcciones

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


class ArgsSuperficie(BaseModel):
    cordillera: str = Field(description="Cordillera sin la palabra 'cordillera', por ejemplo 'blanca'.")
    anio_1: int = Field(default=2018, description="Año inicial.")
    anio_2: int = Field(default=2023, description="Año final.")


# ======================================================================
# Herramientas
# ======================================================================
class CajaHerramientas:
    def __init__(self, motor):
        self.m = motor
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
             "Devuelve lo que el grafo sabe de entidades concretas (lagunas, glaciares, poblados…), con documento y página."),
            ("camino_entidades", self.camino_entidades, ArgsCamino,
             "Encuentra la cadena de relaciones más corta que conecta dos entidades. Úsala cuando preguntan cómo se relacionan."),
            ("consulta_libre", self.consulta_libre, ArgsConsultaLibre,
             "Escribe y ejecuta una consulta Cypher nueva, validada y de solo lectura. Úsala solo si ninguna consulta verificada "
             "cubre los filtros de la pregunta."),
            ("buscar_documentos", self.buscar_documentos, ArgsDocumentos,
             "Busca fragmentos en los 398 documentos del corpus (semántica y palabras clave). Úsala para explicar causas, "
             "contexto, definiciones o hechos históricos, y para complementar los hechos del grafo."),
            ("comparar_superficie", self.comparar_superficie, ArgsSuperficie,
             "Calcula el cambio de la superficie glaciar de una cordillera entre dos inventarios."),
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
        return self._a_evidencia(d, plan, "Consulta nueva (validada, solo lectura)")

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
