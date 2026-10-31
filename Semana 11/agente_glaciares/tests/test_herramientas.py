# -*- coding: utf-8 -*-
import pandas as pd
import pytest

import graphrag_glaciares as GR
import rag_ligero
from herramientas import Contexto, validar_cypher_estricto


def ctx_de(p="prueba"):
    return Contexto(pregunta=p)


# ---------- esquemas: lo que realmente se envía a los proveedores ----------
def test_esquemas_para_groq(caja):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    nombres = set()
    for t in caja.herramientas:
        s = convert_to_openai_tool(t)["function"]
        nombres.add(s["name"])
        assert s["description"] and s["parameters"]["type"] == "object"
    assert nombres == {"buscar_entidad", "consulta_verificada", "ficha_entidad", "camino_entidades", "consulta_libre", "buscar_documentos", "comparar_superficie"}


def test_esquemas_para_gemini(caja):
    from langchain_google_genai import ChatGoogleGenerativeAI
    con = ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key="sin-uso").bind_tools(caja.herramientas)
    assert len(con.kwargs["tools"]) >= 1


def test_esquemas_para_groq_con_el_cliente_real(caja):
    from langchain_groq import ChatGroq
    con = ChatGroq(model="openai/gpt-oss-120b", api_key="sin-uso").bind_tools(caja.herramientas)
    assert {t["function"]["name"] for t in con.kwargs["tools"]} == set(caja.por_nombre)


def test_catalogo_de_consultas_en_la_descripcion(caja):
    d = caja.por_nombre["consulta_verificada"].description
    assert all(k in d for k in GR.ROTULOS)


# ---------- consultas verificadas ----------
def test_consulta_verificada_numera_y_cita(caja):
    ctx = ctx_de("¿Qué lagunas de Áncash tienen riesgo muy alto?")
    t = caja.ejecutar("consulta_verificada", {"consulta_id": "P2", "departamento": "ancash", "nivel": "muy alto"}, ctx)
    assert "[1] (grafo)" in t and len(ctx.fuentes) == 5
    assert {"Palcacocha", "Parón"} <= {x for f in ctx.fuentes for x in [f["texto"].split(" · ")[0].replace("laguna: ", "")]}
    assert all(f["documento"] and f["referencia"] for f in ctx.fuentes)
    assert ctx.pasos[-1]["evidencia_nueva"] == 5


def test_consulta_verificada_toma_filtros_de_la_pregunta_si_el_llm_los_omite(caja):
    ctx = ctx_de("¿Qué lagunas de Cusco tienen riesgo muy alto?")
    caja.ejecutar("consulta_verificada", {"consulta_id": "P2"}, ctx)
    assert all("Áncash" not in f["texto"] for f in ctx.fuentes)


def test_informa_cuando_recorta_filas(caja):
    ctx = ctx_de()
    t = caja.ejecutar("consulta_verificada", {"consulta_id": "P4"}, ctx)
    assert "82 filas" in t and "se muestran 20" in t
    assert len(ctx.fuentes) == 20


@pytest.mark.parametrize("args,texto", [
    ({"consulta_id": "P99"}, "no existe"),
    ({"consulta_id": "P2", "departamento": "Narnia"}, "no es un departamento"),
    ({"consulta_id": "P1", "anio": "dos mil"}, "ERROR"),
    ({}, "ERROR"),
])
def test_argumentos_malos_vuelven_como_error_al_llm(caja, args, texto):
    ctx = ctx_de()
    r = caja.ejecutar("consulta_verificada", args, ctx)
    assert r.startswith("ERROR") and texto in r
    assert ctx.pasos[-1]["error"] and not ctx.fuentes


def test_herramienta_inexistente(caja):
    ctx = ctx_de()
    assert caja.ejecutar("borrar_grafo", {}, ctx).startswith("ERROR")


def test_llamada_repetida_no_duplica_evidencia(caja):
    ctx = ctx_de()
    a = {"consulta_id": "P2", "departamento": "Áncash", "nivel": "Muy alto"}
    caja.ejecutar("consulta_verificada", a, ctx)
    t = caja.ejecutar("consulta_verificada", a, ctx)
    assert "Ya ejecutaste" in t and len(ctx.fuentes) == 5 and ctx.pasos[-1]["repetida"]


def test_numeracion_es_global_entre_herramientas(caja):
    ctx = ctx_de()
    caja.ejecutar("consulta_verificada", {"consulta_id": "P2", "departamento": "Áncash", "nivel": "Muy alto"}, ctx)
    t = caja.ejecutar("buscar_documentos", {"consulta": "laguna Palcacocha peligro de desborde"}, ctx)
    assert "[6] (texto)" in t
    assert [f["n"] for f in ctx.fuentes] == list(range(1, len(ctx.fuentes) + 1))


# ---------- ficha, camino, entidades ----------
@pytest.mark.parametrize("nombre", ["Palcacocha", "laguna Palcacocha", "Laguna de Palcacocha", "Palcacoha", "PALCACOCHA"])
def test_ficha_reconoce_variantes_del_nombre(caja, nombre):
    ctx = ctx_de()
    t = caja.ejecutar("ficha_entidad", {"nombres": [nombre]}, ctx)
    assert "Ficha de Palcacocha" in t and ctx.fuentes


def test_ficha_filtra_relaciones(caja):
    ctx = ctx_de()
    caja.ejecutar("ficha_entidad", {"nombres": ["Palcacocha"], "relaciones": ["amenazaA"]}, ctx)
    assert ctx.fuentes and all("amenazaA" in f["texto"] for f in ctx.fuentes)


def test_relaciones_de_la_descripcion_existen_en_el_grafo():
    """Prueba real: la descripción decía tieneObra y el grafo usa tieneObraSeguridad; el LLM repetía llamadas vacías."""
    import os
    import re
    from conftest import DATOS
    from herramientas import ArgsFicha
    existentes = set(pd.read_csv(os.path.join(DATOS, "relaciones.csv"), usecols=["pred"]).pred)
    texto = ArgsFicha.model_fields["relaciones"].description
    nombradas = re.findall(r"\b[a-z]+[A-Z]\w*\b", texto)
    assert len(nombradas) >= 10 and set(nombradas) <= existentes


def test_ficha_con_obras_de_seguridad(caja):
    ctx = ctx_de()
    caja.ejecutar("ficha_entidad", {"nombres": ["Palcacocha"], "relaciones": ["tieneObraSeguridad"]}, ctx)
    assert ctx.fuentes and all("tieneObraSeguridad" in f["texto"] for f in ctx.fuentes)


def test_filtro_vacio_indica_las_relaciones_que_existen(caja):
    ctx = ctx_de()
    t = caja.ejecutar("ficha_entidad", {"nombres": ["Palcacocha"], "relaciones": ["noExiste"]}, ctx)
    assert "0 hechos" in t and "tieneObraSeguridad" in t and not ctx.fuentes


def test_ficha_de_entidad_desconocida(caja):
    t = caja.ejecutar("ficha_entidad", {"nombres": ["Atlantida del Sur"]}, ctx_de())
    assert "No encontré" in t and "buscar_entidad" in t


def test_camino(caja):
    ctx = ctx_de()
    t = caja.ejecutar("camino_entidades", {"a": "nevado Huascarán", "b": "Yungay"}, ctx)
    assert "Camino de" in t and ctx.fuentes


def test_buscar_entidad(caja):
    t = caja.ejecutar("buscar_entidad", {"nombre": "Palcacoha"}, ctx_de())
    assert "Palcacocha" in t


# ---------- texto ----------
def test_buscar_documentos_con_filtros(caja, indice):
    ctx = ctx_de()
    caja.ejecutar("buscar_documentos", {"consulta": "situación de la laguna Parón", "fuente": "ANA", "anio_max": 2010}, ctx)
    assert ctx.fuentes and all(f["tipo"] == "texto" for f in ctx.fuentes)
    d = indice.buscar("situación de la laguna Parón", k=10, filtros={"fuente": ["ANA"], "anio_max": 2010})
    assert len(d) and set(d.fuente) == {"ANA"} and d.year.max() <= 2010


def test_referencia_con_paginas():
    f = pd.DataFrame([{"title": "Informe", "fuente": "ANA", "year": 2019, "page_start": 3.0, "page_end": 7.0, "paper_id": "x"}]).iloc[0]
    assert rag_ligero.referencia(f) == "Informe — ANA, 2019, pp. 3–7"


def test_bm25_prefiere_el_documento_correcto():
    b = rag_ligero.BM25Disperso(["laguna palcacocha riesgo", "glaciar pastoruri retroceso", "laguna paron obras"])
    assert b.puntajes("palcacocha").argmax() == 0
    assert b.puntajes("xyz").sum() == 0


# ---------- superficie ----------
def test_comparar_superficie(caja):
    ctx = ctx_de()
    t = caja.ejecutar("comparar_superficie", {"cordillera": "Cordillera Blanca", "anio_1": 2018, "anio_2": 2023}, ctx)
    assert "449.75" in t and "424.86" in t and "-24.89" in t


# ---------- seguridad de las consultas escritas por el LLM ----------
@pytest.mark.parametrize("mala", [
    "MATCH (n) DETACH DELETE n RETURN n",
    "MATCH (n) SET n.x = 1 RETURN n",
    "CREATE (n:Entidad) RETURN n",
    "MATCH (n) RETURN n; MATCH (m) DELETE m",
    "MATCH (n) /* DELETE */ RETURN n",
    "MATCH (n) RETURN n // SET",
    "CALL dbms.components() YIELD name MATCH (n) RETURN name",
    "MATCH (n) RETURN apoc.version()",
    "MATCH (n) RETURN dbms.x()",
    "USE system MATCH (n) RETURN n",
    "LOAD CSV FROM 'http://x' AS l MATCH (n) RETURN n",
    "RETURN 1",
    "",
])
def test_cypher_peligroso_se_rechaza(mala):
    with pytest.raises(ValueError):
        validar_cypher_estricto(mala)


def test_cypher_de_lectura_pasa_con_limite():
    c = validar_cypher_estricto("MATCH (l:Laguna {k: 'palcacocha'}) RETURN l.nombre AS laguna", 50)
    assert c.endswith("LIMIT 50")
    assert validar_cypher_estricto("MATCH (l:Laguna) RETURN l LIMIT 900", 50).endswith("LIMIT 50")


def test_nombre_con_palabra_prohibida_dentro_de_cadena_no_molesta():
    assert validar_cypher_estricto("MATCH (l {nombre: 'Show; create'}) RETURN l")


def test_consulta_libre_no_disponible_en_la_copia(caja):
    t = caja.ejecutar("consulta_libre", {"pedido": "lagunas"}, ctx_de())
    assert t.startswith("ERROR") and "Neo4j" in t
