"use strict";
// Página del agente: todo el contenido del servidor se inserta como texto, nunca como HTML.

const EJEMPLOS = [
  "¿Qué lagunas de Áncash tienen riesgo muy alto de desborde?",
  "¿Por qué la laguna Palcacocha es peligrosa?",
  "¿Qué lagunas en riesgo no tienen obras de seguridad y qué poblados amenazan?",
  "¿Cómo se relaciona el nevado Huascarán con Yungay?",
  "¿Cómo cambió la superficie glaciar de la Cordillera Blanca entre 2018 y 2023?",
];
const NOMBRES = {
  buscar_entidad: "Buscar entidad en el catálogo",
  consulta_verificada: "Consulta verificada al grafo",
  ficha_entidad: "Ficha de entidad en el grafo",
  camino_entidades: "Camino entre entidades",
  consulta_libre: "Consulta nueva al grafo",
  buscar_documentos: "Búsqueda en los documentos",
  comparar_superficie: "Cálculo de superficie glaciar",
};

const MOTOR = { ficha: "ficha_entidad", camino: "camino_entidades", texto: "buscar_documentos", cypher: "consulta_libre" };
// La ruta de respaldo usa los nombres del motor de la semana 10: P2, E3_conteo…, ficha, camino, texto.
const nombreHerramienta = (h) => NOMBRES[h] || NOMBRES[MOTOR[h]] || `Consulta verificada al grafo (${String(h).split("_")[0]})`;
const $ = (id) => document.getElementById(id);
const crear = (etiqueta, clase, texto) => {
  const e = document.createElement(etiqueta);
  if (clase) e.className = clase;
  if (texto !== undefined) e.textContent = texto;
  return e;
};
const leer = (almacen, k) => { try { return almacen.getItem(k); } catch { return null; } };
const guardar = (almacen, k, v) => { try { almacen.setItem(k, v); } catch { /* sin almacenamiento: sigue funcionando */ } };

function idSesion() {
  let s = leer(sessionStorage, "sesion");
  if (!s) {
    s = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9_-]/g, "");
    guardar(sessionStorage, "sesion", s);
  }
  return s;
}

// ---------- estado del servicio ----------
// Solo se repite mientras el servicio despierta: una consulta periódica con la pestaña abierta
// impediría que la nube lo apague cuando nadie pregunta, y seguiría gastando crédito.
async function comprobarEstado() {
  let reintento = 5000;
  try {
    const r = await fetch("/api/info");
    const d = await r.json();
    const caja = $("estado");
    caja.className = "estado " + (d.estado === "listo" ? "listo" : d.estado === "error" ? "error" : "");
    if (d.estado === "listo") {
      const llm = (d.modelos || []).some((m) => m.disponible);
      $("estado-texto").textContent = `${d.grafo.activo} · ${llm ? "LLM disponible" : "sin LLM: modo evidencia"}`;
      return;
    } else if (d.estado === "error") {
      $("estado-texto").textContent = "No pudo iniciar";
      return;
    } else {
      $("estado-texto").textContent = `Despertando: ${d.detalle}…`;
    }
  } catch {
    $("estado").className = "estado error";
    $("estado-texto").textContent = "Sin conexión";
    reintento = 15000;
  }
  if (!document.hidden) setTimeout(comprobarEstado, reintento);
}

// ---------- texto con formato ----------
function enLinea(padre, texto) {
  const partes = texto.split(/(\*\*[^*]+\*\*|`[^`]+`|\[\d+\])/g);
  for (const p of partes) {
    if (!p) continue;
    if (p.startsWith("**") && p.endsWith("**")) padre.append(crear("strong", "", p.slice(2, -2)));
    else if (p.startsWith("`") && p.endsWith("`")) padre.append(crear("code", "", p.slice(1, -1)));
    else if (/^\[\d+\]$/.test(p)) {
      const n = p.slice(1, -1);
      const b = crear("button", "cita", n);
      b.type = "button";
      b.setAttribute("aria-label", `Ver la fuente ${n}`);
      b.addEventListener("click", () => resaltarFuente(n));
      padre.append(b);
    } else padre.append(document.createTextNode(p));
  }
}

function formatear(contenedor, texto) {
  let lista = null;
  for (const linea of texto.split("\n")) {
    const vineta = linea.match(/^\s*[-*•]\s+(.*)$/);
    if (vineta) {
      if (!lista) { lista = crear("ul"); contenedor.append(lista); }
      const li = crear("li");
      enLinea(li, vineta[1]);
      lista.append(li);
    } else {
      lista = null;
      if (linea.trim()) {
        const p = crear("p");
        enLinea(p, linea.trim());
        contenedor.append(p);
      }
    }
  }
}

// ---------- panel lateral ----------
function textoArgs(args) {
  const v = Object.entries(args || {}).filter(([, x]) => x !== null && x !== undefined && x !== "");
  return v.map(([k, x]) => `${k}: ${Array.isArray(x) ? x.join(", ") : x}`).join(" · ");
}

function agregarPaso(p) {
  const lista = $("pasos");
  const li = crear("li", "paso");
  if (p.tipo === "herramienta") {
    li.classList.add("herramienta");
    if (p.error) li.classList.add("fallo");
    li.append(crear("strong", "", nombreHerramienta(p.herramienta)));
    const detalle = [textoArgs(p.args)];
    if (p.error) detalle.push(`falló: ${p.error}`);
    else if (p.repetida) detalle.push("ya estaba ejecutada");
    else if (p.evidencia_nueva !== undefined) detalle.push(`${p.evidencia_nueva} evidencias nuevas`);
    if (p.ms) detalle.push(`${(p.ms / 1000).toFixed(1)} s`);
    li.append(crear("small", "", detalle.filter(Boolean).join(" · ")));
  } else if (p.tipo === "decision") {
    li.append(crear("strong", "", p.herramientas.length ? "El agente decide qué consultar" : "El agente redacta la respuesta"));
    li.append(crear("small", "", p.modelo || ""));
  } else if (p.tipo === "cypher") {
    li.append(crear("strong", "", "Consulta escrita y validada"));
    const d = crear("details");
    d.append(crear("summary", "", "Ver Cypher"), crear("pre", "", p.cypher || ""));
    li.append(d);
  } else if (p.tipo === "verificacion") {
    li.append(crear("strong", "", "Verificación"), crear("small", "", p.resultado));
  } else if (p.tipo === "sintesis") {
    li.append(crear("strong", "", "Redacta con la evidencia reunida"), crear("small", "", p.modelo || ""));
  } else if (p.tipo === "guardia") {
    li.append(crear("strong", "", "Filtro inicial"), crear("small", "", p.motivo));
  } else if (p.tipo === "respaldo") {
    li.classList.add("fallo");
    li.append(crear("strong", "", "Ruta de respaldo"), crear("small", "", "El LLM con herramientas no respondió"));
  } else return;
  lista.append(li);
}

function mostrarFuentes(fuentes) {
  const lista = $("fuentes");
  lista.replaceChildren();
  const citadas = (fuentes || []).filter((f) => f.citada);
  if (!citadas.length) {
    lista.append(crear("li", "vacio", "Esta respuesta no cita fuentes."));
    return;
  }
  for (const f of citadas) {
    const li = crear("li", "fuente");
    li.id = `fuente-${f.n}`;
    const cab = crear("div");
    cab.append(crear("span", "n", `[${f.n}]`), crear("span", "etiqueta", f.tipo === "grafo" ? "grafo" : "texto"));
    li.append(cab);
    const ref = crear("p");
    if (f.url && /^https:\/\//.test(f.url)) {
      const a = crear("a", "", f.referencia);
      a.href = f.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      ref.append(a);
    } else ref.textContent = f.referencia;
    li.append(ref);
    const cita = f.evidencia || (f.tipo === "grafo" ? f.texto : f.texto.slice(0, 220) + (f.texto.length > 220 ? "…" : ""));
    if (cita) li.append(crear("blockquote", "", cita));
    lista.append(li);
  }
}

function resaltarFuente(n) {
  document.querySelectorAll(".fuente.resaltada").forEach((e) => e.classList.remove("resaltada"));
  const f = $(`fuente-${n}`);
  if (f) {
    f.classList.add("resaltada");
    f.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

// ---------- conversación ----------
function agregarMensaje(clase, texto) {
  const b = $("bienvenida");
  if (b) b.remove();
  const m = crear("div", `mensaje ${clase}`, texto);
  $("mensajes").append(m);
  m.scrollIntoView({ block: "end" });
  return m;
}

function mostrarRespuesta(burbuja, r) {
  burbuja.className = "mensaje agente";
  burbuja.textContent = "";
  formatear(burbuja, r.respuesta);
  for (const a of r.avisos || []) burbuja.append(crear("div", "aviso", a));
  const modos = { agente: "agente", graphrag: "motor GraphRAG (respaldo)", evidencia: "solo evidencia (sin LLM)", guardia: "sin LLM" };
  const meta = [modos[r.modo] || r.modo, r.modelo, `${r.segundos} s`];
  if (r.desde_cache) meta.push("desde caché");
  if (r.autocorregida) meta.push("corregida tras la verificación");
  burbuja.append(crear("div", "meta", meta.filter(Boolean).join(" · ")));
  mostrarFuentes(r.fuentes);
  burbuja.scrollIntoView({ block: "end" });
}

function cabeceras() {
  const h = { "Content-Type": "application/json" };
  const clave = leer(sessionStorage, "clave");
  if (clave) h["X-API-Key"] = clave;
  return h;
}

async function preguntar(texto) {
  const boton = $("enviar");
  boton.disabled = true;
  agregarMensaje("usuario", texto);
  const burbuja = agregarMensaje("agente pensando", "Pensando…");
  $("pasos").replaceChildren();
  $("fuentes").replaceChildren(crear("li", "vacio", "Buscando…"));
  try {
    let r = await fetch("/api/preguntar/stream", { method: "POST", headers: cabeceras(), body: JSON.stringify({ pregunta: texto, sesion_id: idSesion() }) });
    if (r.status === 401) {
      const clave = window.prompt("Este agente requiere una clave de acceso:");
      if (clave) {
        guardar(sessionStorage, "clave", clave);
        r = await fetch("/api/preguntar/stream", { method: "POST", headers: cabeceras(), body: JSON.stringify({ pregunta: texto, sesion_id: idSesion() }) });
      }
    }
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(typeof d.detail === "string" ? d.detail : "La pregunta no se pudo procesar.");
    }
    await leerEventos(r, burbuja);
  } catch (e) {
    burbuja.className = "mensaje agente";
    burbuja.textContent = "";
    burbuja.append(crear("div", "aviso", e.message || "No se pudo conectar con el agente."));
    $("fuentes").replaceChildren(crear("li", "vacio", "Sin fuentes."));
  } finally {
    boton.disabled = false;
    $("pregunta").focus();
    comprobarEstado();   // el servicio ya está despierto: se aprovecha para actualizar el indicador
  }
}

async function leerEventos(respuesta, burbuja) {
  const lector = respuesta.body.getReader();
  const decodificador = new TextDecoder();
  let resto = "";
  let terminado = false;
  while (true) {
    const { done, value } = await lector.read();
    if (done) break;
    resto += decodificador.decode(value, { stream: true });
    const bloques = resto.split("\n\n");
    resto = bloques.pop();
    for (const b of bloques) {
      if (!b.startsWith("data: ")) continue;
      const ev = JSON.parse(b.slice(6));
      if (ev.tipo === "paso") {
        agregarPaso(ev.paso);
        if (ev.paso.tipo === "herramienta") burbuja.textContent = `Consultando: ${nombreHerramienta(ev.paso.herramienta)}…`;
      } else if (ev.tipo === "final") {
        terminado = true;
        if (!$("pasos").children.length) (ev.resultado.pasos || []).forEach(agregarPaso);
        mostrarRespuesta(burbuja, ev.resultado);
      } else if (ev.tipo === "error") {
        throw new Error(ev.detalle);
      }
    }
  }
  if (!terminado) throw new Error("La conexión se cortó antes de terminar. Vuelve a preguntar.");
}

// ---------- inicio ----------
for (const q of EJEMPLOS) {
  const b = crear("button", "", q);
  b.type = "button";
  b.addEventListener("click", () => preguntar(q));
  $("ejemplos").append(b);
}
$("formulario").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("pregunta").value.trim();
  if (t.length < 2 || $("enviar").disabled) return;
  $("pregunta").value = "";
  preguntar(t);
});
$("pregunta").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("formulario").requestSubmit();
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("estado").classList.contains("listo")) comprobarEstado();
});
comprobarEstado();
