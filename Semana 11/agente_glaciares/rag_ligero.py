# -*- coding: utf-8 -*-
"""Buscador de texto para producción. Reutiliza los fragmentos y embeddings bge-m3 de la semana 9 sin recalcular nada.

Misma interfaz que el índice de la semana 9 (buscar, cfg, referencia), con dos cambios para correr en un contenedor pequeño:
  · 30 mil vectores se comparan en milisegundos con NumPy: Chroma no hace falta y no ocuparía memoria ni tiempo de arranque;
  · BM25 usa una matriz dispersa: ocupa una fracción de la memoria de rank_bm25 y responde más rápido.
Si el modelo de embeddings no se puede cargar, sigue funcionando solo con BM25.
"""
import json
import os
import re
import threading
from collections import Counter

import numpy as np
import pandas as pd
from scipy import sparse

from graphrag_glaciares import norm

COLUMNAS = ["frag_id", "tipo", "paper_id", "page_start", "page_end", "heading", "text", "title", "fuente", "year", "pdf_url", "uri", "idioma"]
VACIAS = set("""de la el en y a los las del se que por con para una un su al lo como mas o es son the of and in to for on with
    this that from by an are was were sobre entre esta este estos estas sus ha han fue fueron""".split())
RRF_K = 60


def tokens(t):
    return [x for x in re.findall(r"[a-z0-9]{2,}", norm(t)) if x not in VACIAS]


class BM25Disperso:
    def __init__(self, textos, k1=1.5, b=0.75):
        vocab, filas, cols, datos = {}, [], [], []
        largos = np.zeros(len(textos), dtype=np.float32)
        for i, t in enumerate(textos):
            c = Counter(tokens(t))
            largos[i] = sum(c.values())
            for w, n in c.items():
                filas.append(i)
                cols.append(vocab.setdefault(w, len(vocab)))
                datos.append(n)
        tf = sparse.csr_matrix((np.asarray(datos, dtype=np.float32), (filas, cols)), shape=(len(textos), len(vocab)))
        df = np.bincount(tf.indices, minlength=len(vocab))
        self.idf = np.log(1 + (len(textos) - df + 0.5) / (df + 0.5)).astype(np.float32)
        tf = tf.tocoo()
        norma = k1 * (1 - b + b * largos[tf.row] / max(float(largos.mean()), 1.0))
        self.pesos = sparse.csc_matrix((tf.data * (k1 + 1) / (tf.data + norma), (tf.row, tf.col)), shape=tf.shape)
        self.vocab = vocab

    def puntajes(self, consulta):
        ids = [self.vocab[w] for w in set(tokens(consulta)) if w in self.vocab]
        if not ids:
            return np.zeros(self.pesos.shape[0], dtype=np.float32)
        return np.asarray(self.pesos[:, ids].dot(self.idf[ids])).ravel()


class EmbedderBGE:
    def __init__(self, modelo="BAAI/bge-m3"):
        from sentence_transformers import SentenceTransformer
        self.nombre = modelo
        self.m = SentenceTransformer(modelo, device="cpu")
        self.m.max_seq_length = 512
        self._candado = threading.Lock()

    def consulta(self, texto):
        with self._candado:   # varias preguntas a la vez no deben competir por los mismos núcleos
            v = self.m.encode(texto, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        return v.astype(np.float32)


class IndiceLigero:
    def __init__(self, frag, emb, embedder=None, umbral=0.58):
        if len(frag) != len(emb):
            raise ValueError(f"fragmentos ({len(frag)}) y embeddings ({len(emb)}) no coinciden")
        self.frag = frag.reset_index(drop=True)
        self.emb = np.asarray(emb, dtype=np.float32)
        self.embedder = embedder
        self.bm25 = BM25Disperso(frag["texto_indexar"].tolist() if "texto_indexar" in frag else frag["text"].tolist())
        self.frag = self.frag[[c for c in COLUMNAS if c in self.frag]]
        self.cfg = {"UMBRAL_SIM": umbral if embedder is not None else None}

    def _mascara(self, filtros):
        if not filtros:
            return None
        m = np.ones(len(self.frag), dtype=bool)
        if filtros.get("fuente"):
            m &= self.frag.fuente.isin(filtros["fuente"]).to_numpy()
        if filtros.get("anio_min"):
            m &= (self.frag.year >= int(filtros["anio_min"])).to_numpy()
        if filtros.get("anio_max"):
            m &= (self.frag.year <= int(filtros["anio_max"])).to_numpy()
        return m

    def buscar(self, consulta, k=8, modo="hibrido", filtros=None):
        """Devuelve los k mejores fragmentos con sus columnas más sim (coseno) y bm25, en orden de relevancia."""
        mascara = self._mascara(filtros)
        n = len(self.frag)
        sim = self.emb @ self.embedder.consulta(consulta) if self.embedder is not None and modo != "bm25" else None
        bm = self.bm25.puntajes(consulta) if modo != "semantico" or sim is None else None
        if mascara is not None:
            if sim is not None:
                sim = np.where(mascara, sim, -1.0)
            if bm is not None:
                bm = np.where(mascara, bm, 0.0)
        profundo = min(n, max(100, 5 * k))
        def rangos(valores):
            orden = np.argpartition(-valores, profundo - 1)[:profundo]
            orden = orden[np.argsort(-valores[orden])]
            if mascara is not None:
                orden = orden[mascara[orden]]
            return orden
        puntos = np.zeros(n, dtype=np.float64)
        if sim is not None:
            orden = rangos(sim)
            puntos[orden] += 1.0 / (RRF_K + np.arange(len(orden)) + 1)
        if bm is not None:
            orden = rangos(bm)
            orden = orden[bm[orden] > 0]
            puntos[orden] += 1.0 / (RRF_K + np.arange(len(orden)) + 1)
        elegidos = np.argsort(-puntos)[:k]
        elegidos = elegidos[puntos[elegidos] > 0]
        d = self.frag.iloc[elegidos].copy()
        d["sim"] = sim[elegidos] if sim is not None else np.nan
        d["bm25"] = bm[elegidos] if bm is not None else np.nan
        return d.reset_index(drop=True)


def referencia(f):
    """'Título — INAIGEM, 2024, p. 68': mismo formato de cita que la semana 9."""
    titulo = str(getattr(f, "title", "") or getattr(f, "paper_id", ""))
    partes = []
    if getattr(f, "fuente", None):
        partes.append(str(f.fuente))
    anio = getattr(f, "year", None)
    if anio is not None and not pd.isna(anio) and int(anio) > 0:
        partes.append(str(int(anio)))
    a, b = getattr(f, "page_start", None), getattr(f, "page_end", None)
    if a is not None and not pd.isna(a):
        a, b = int(a), (int(b) if b is not None and not pd.isna(b) else int(a))
        partes.append(f"p. {a}" if a == b else f"pp. {a}–{b}")
    return titulo + (" — " + ", ".join(partes) if partes else "")


def cargar_indice(carpeta, modelo="BAAI/bge-m3", umbral=0.58, log=print):
    """Carga los fragmentos y embeddings de la semana 9. Con EMBEDDER=ninguno o si el modelo falla, queda solo BM25."""
    info = json.load(open(os.path.join(carpeta, "embeddings_info.json"), encoding="utf-8"))
    frag = pd.read_parquet(os.path.join(carpeta, "fragmentos_rag.parquet"))
    emb = np.load(os.path.join(carpeta, "embeddings.npy"), mmap_mode="r")
    embedder = None
    if str(modelo).lower() not in ("ninguno", "none", ""):
        if modelo != info.get("modelo"):
            raise ValueError(f"Los embeddings se calcularon con {info.get('modelo')}, no con {modelo}")
        try:
            embedder = EmbedderBGE(modelo)
        except Exception as e:
            log(f"Modelo de embeddings no disponible ({str(e)[:160]}); búsqueda solo con BM25")
    return IndiceLigero(frag, emb, embedder, umbral)
