# -*- coding: utf-8 -*-
"""La copia embebida debe dar los mismos resultados que Neo4j.
La referencia son las filas de un Neo4j 5 cargado con estos mismos CSV (scripts/cargar_neo4j.py). El reporte de la semana 10
da 15 filas en P5: su instancia de Aura tenía 37 264 relaciones y estos CSV tienen 36 596."""
import os

import pytest

import graphrag_glaciares as GR
from config import Config
from grafo_resiliente import GrafoKuzuSeguro, GrafoNeo4jSeguro, GrafoResiliente, _sin_direcciones

FILAS_NEO4J = {"P1": 5, "P2": 5, "P3": 72, "P4": 82, "P5": 16, "P6": 37, "P7": 18, "P8": 5, "P9": 19, "P10": 66,
               "E1_negacion_sin_obras": 6, "E2_negacion_sin_monitoreo": 10, "E3_conteo_departamento": 7, "E4_conteo_cordillera": 13,
               "E5_ranking_volumen": 5, "E6_linea_tiempo": 71, "E7_instituciones": 10, "E11_poblados_y_glaciares": 17}
URI_PRUEBA = os.environ.get("NEO4J_PRUEBA_URI")
con_neo4j = pytest.mark.skipif(not URI_PRUEBA, reason="sin NEO4J_PRUEBA_URI (Neo4j local de prueba)")


@pytest.mark.parametrize("consulta,filas", FILAS_NEO4J.items())
def test_consultas_verificadas_igual_que_neo4j(kuzu_solo, consulta, filas):
    c = GR.PLANTILLAS[consulta]
    assert len(kuzu_solo.q(c["cypher"], **c.get("params", {}))) == filas


def test_sin_base_previa_tambien_arranca(tmp_path):
    from conftest import DATOS
    import shutil
    for n in ("nodos.csv", "relaciones.csv"):
        shutil.copy(os.path.join(DATOS, n), tmp_path / n)
    g = GrafoKuzuSeguro(str(tmp_path), ruta_db=str(tmp_path / "base"), log=lambda m: None)
    assert len(g.q("MATCH (l:Laguna) RETURN l.nombre AS n LIMIT 2")) == 2


def test_conteo_por_departamento_con_filtro(kuzu_solo):
    c = GR.PLANTILLAS["E3_conteo_departamento"]
    assert len(kuzu_solo.q(c["cypher"], niveles=["Muy alto"])) == 2


@pytest.fixture(scope="module")
def neo4j_prueba():
    g = GrafoNeo4jSeguro(URI_PRUEBA, "neo4j", os.environ["NEO4J_PRUEBA_CLAVE"], timeout=30)
    yield g
    g.cerrar()


@con_neo4j
@pytest.mark.parametrize("consulta,filas", FILAS_NEO4J.items())
def test_neo4j_real_da_lo_mismo(neo4j_prueba, consulta, filas):
    c = GR.PLANTILLAS[consulta]
    assert len(neo4j_prueba.q(c["cypher"], **c.get("params", {}))) == filas


@con_neo4j
@pytest.mark.parametrize("mala", ["CREATE (x:Intruso) RETURN x", "MATCH (n:Entidad {k: 'palcacocha'}) SET n.hack = 1 RETURN n"])
def test_neo4j_rechaza_escrituras_aunque_pasen_la_validacion(neo4j_prueba, mala):
    with pytest.raises(Exception, match="AccessMode|read access"):
        neo4j_prueba.q(mala)


@con_neo4j
def test_neo4j_corta_consultas_lentas():
    g = GrafoNeo4jSeguro(URI_PRUEBA, "neo4j", os.environ["NEO4J_PRUEBA_CLAVE"], timeout=1)
    with pytest.raises(Exception, match="TimedOut|timed out"):
        g.q("MATCH (a)-[*..6]-(b) RETURN count(*) AS n")
    g.cerrar()


@con_neo4j
def test_neo4j_camino_igual_de_largo_que_la_copia(neo4j_prueba, grafo, motor_base):
    a = motor_base.enl.por_k["huascaran"].iloc[0].id
    b = motor_base.enl.por_k["yungay"].iloc[0].id
    assert len(neo4j_prueba.camino(a, b)) == len(grafo.camino(a, b))


def test_camino_y_ficha(grafo, motor_base):
    a = motor_base.enl.por_k["huascaran"].iloc[0].id
    b = motor_base.enl.por_k["yungay"].iloc[0].id
    assert len(grafo.camino(a, b)) >= 1
    assert len(grafo.ficha([motor_base.enl.por_k["palcacocha"].iloc[0].id], 30)) > 0


def test_resiliente_sin_neo4j_usa_copia(grafo):
    assert "Kùzu" in grafo.nombre
    assert grafo.estado()["neo4j_conectado"] is False


def test_neo4j_inalcanzable_no_impide_arrancar(tmp_path):
    from conftest import DATOS
    cfg = Config(neo4j_uri="neo4j+s://no-existe.invalid", neo4j_clave="x", datos_dir=DATOS, reintento_neo4j=9999)
    g = GrafoResiliente(cfg, log=lambda m: None)
    assert "Kùzu" in g.nombre
    e = g.estado()
    assert e["neo4j_configurado"] is True and e["neo4j_conectado"] is False
    assert "no-existe" not in (e["ultimo_error"] or "")   # la dirección de la base no se expone
    assert len(g.q("MATCH (l:Laguna) RETURN l.nombre AS n LIMIT 3")) == 3


class _Principal:
    nombre, cypher_ok = "Neo4j", True

    def __init__(self, error=None):
        self.error, self.llamadas = error, 0

    def q(self, cypher, **p):
        self.llamadas += 1
        if self.error:
            raise self.error
        return "principal"


class _Respaldo:
    nombre, cypher_ok = "copia", True

    def q(self, cypher, **p):
        return "respaldo"


def test_caida_durante_el_servicio_cambia_al_respaldo_sin_perder_la_pregunta():
    from neo4j.exceptions import ServiceUnavailable
    g = GrafoResiliente.__new__(GrafoResiliente)
    import threading
    g.cfg, g.log = Config(reintento_neo4j=9999), lambda m: None
    g._principal, g._respaldo = _Principal(ServiceUnavailable("bolt://secreto:7687 caído")), _Respaldo()
    g._caido_desde, g._ultimo_error, g._candado = None, None, threading.RLock()
    assert g.q("MATCH") == "respaldo"
    assert g._caido_desde is not None and "secreto" not in g._ultimo_error
    assert g.q("MATCH") == "respaldo"
    assert g._principal.llamadas == 1   # no se insiste con la base caída


def test_error_de_consulta_no_cambia_de_base():
    from neo4j.exceptions import CypherSyntaxError
    g = GrafoResiliente.__new__(GrafoResiliente)
    import threading
    g.cfg, g.log = Config(), lambda m: None
    g._principal, g._respaldo = _Principal(CypherSyntaxError("mal escrita")), _Respaldo()
    g._caido_desde, g._ultimo_error, g._candado = None, None, threading.RLock()
    with pytest.raises(CypherSyntaxError):
        g.q("MATCH")
    assert g._caido_desde is None


def test_sin_direcciones():
    assert "abc.databases" not in _sin_direcciones("fallo en neo4j+s://abc.databases.neo4j.io:7687 ahora")
    assert "abc.databases" not in _sin_direcciones("Failed to DNS resolve address abc.databases.neo4j.io:7687: error")


def test_base_previa_es_de_solo_lectura(kuzu_solo):
    with pytest.raises(Exception):
        kuzu_solo.c.execute("CREATE NODE TABLE Intruso(id STRING, PRIMARY KEY(id))")
