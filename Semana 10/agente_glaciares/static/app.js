"use strict";
// Página del agente: todo el contenido del servidor se inserta como texto, nunca como HTML.

const EJEMPLOS = [
  "¿Qué lagunas de Áncash tienen riesgo muy alto de desborde?",
  "¿Por qué la laguna Palcacocha es peligrosa?",
  "¿Qué lagunas en riesgo no tienen obras de seguridad y qué poblados amenazan?",
  "¿Cuántas lagunas hay en el Perú?",
  "Muéstrame en un mapa las lagunas en riesgo",
  "Quiero ver Parón en 3D",
  "Grafica cómo cambió la superficie glaciar de la Cordillera Blanca",
  "Dame la ficha de riesgo de la laguna Palcacocha",
];
const NOMBRES = {
  buscar_entidad: "Buscar entidad en el catálogo",
  consulta_verificada: "Consulta verificada al grafo",
  ficha_entidad: "Ficha de entidad en el grafo",
  camino_entidades: "Camino entre entidades",
  consulta_libre: "Consulta nueva al grafo",
  cifras_nacionales: "Cifras oficiales del Perú",
  ranking_lagunas: "Ranking de lagunas",
  ficha_laguna: "Ficha de riesgo de la laguna",
  mostrar_mapa: "Mapa de lagunas",
  vista_3d: "Vista 3D del relieve",
  graficar: "Gráfico",
  ver_subgrafo: "Conexiones en el grafo",
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
  if (r.respuesta.startsWith("No encontré") && r.modo !== "evidencia") {
    const s = crear("div", "sugerencias");
    for (const q of EJEMPLOS.slice(0, 3)) {
      const b = crear("button", "boton-sec", q);
      b.type = "button";
      b.addEventListener("click", () => preguntar(q));
      s.append(b);
    }
    burbuja.append(crear("p", "meta", "Respondo sobre glaciares, lagunas y riesgo de desborde en el Perú. Prueba con:"), s);
  }
  for (const v of r.visualizaciones || []) dibujarVisual(burbuja, v);
  if ((r.mapa_sugerido || []).length) {
    const b = crear("button", "boton-sec", `Ver en el mapa (${r.mapa_sugerido.length})`);
    b.type = "button";
    b.addEventListener("click", () => {
      b.remove();
      dibujarVisual(burbuja, { tipo: "mapa", titulo: "Lagunas de esta respuesta", puntos: r.mapa_sugerido, sin_ubicacion: [], tres_d: false });
    });
    burbuja.append(b);
  }
  const modos = { agente: "agente", graphrag: "motor GraphRAG (respaldo)", evidencia: "solo evidencia (sin LLM)", conversacion: "conversación", general: "conocimiento general, sin fuentes" };
  const meta = [modos[r.modo] || r.modo, r.modelo, `${r.segundos} s`];
  if (r.desde_cache) meta.push("desde caché");
  if (r.autocorregida) meta.push("corregida tras la verificación");
  burbuja.append(crear("div", "meta", meta.filter(Boolean).join(" · ")));
  mostrarFuentes(r.fuentes);
  if (!r.n_citas && (r.visualizaciones || []).some((v) => v.tipo === "mapa")) {
    $("fuentes").replaceChildren(crear("li", "vacio", "Haz clic en un punto del mapa para ver su documento y página."));
  }
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

// ---------- visualizaciones ----------
const SATELITE = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const RELIEVE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
let cargaMapa = null;

function cargarVisorMapa() {
  if (cargaMapa) return cargaMapa;
  cargaMapa = new Promise((listo, fallo) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/static/vendor/maplibre-gl.css";
    document.head.append(css);
    const js = document.createElement("script");
    js.src = "/static/vendor/maplibre-gl.js";
    js.onload = () => listo(window.maplibregl);
    js.onerror = () => { cargaMapa = null; fallo(new Error("No se pudo cargar el visor de mapas.")); };
    document.head.append(js);
  });
  return cargaMapa;
}

const claseNivel = (n) => (n === "Muy alto" ? "nivel-muy-alto" : n === "Alto" ? "nivel-alto" : "nivel-otro");

function popupLaguna(p) {
  const d = crear("div", "popup-mapa");
  d.append(crear("strong", "", p.nombre));
  d.append(crear("div", "", p.nivel ? `Riesgo: ${p.nivel}` : "Sin nivel de riesgo en el grafo"));
  if (p.departamentos && p.departamentos.length) d.append(crear("div", "", p.departamentos.join(", ")));
  if (p.altitud) d.append(crear("div", "", `Relieve en este punto: ${p.altitud} m s. n. m.`));
  if (p.nota) d.append(crear("div", "", p.nota));
  if (p.referencia) {
    const f = crear("div");
    if (p.url && /^https:\/\//.test(p.url)) {
      const a = crear("a", "", p.referencia);
      a.href = p.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      f.append(a);
    } else f.textContent = p.referencia;
    d.append(f);
  }
  return d;
}

async function dibujarMapa(caja, v, formacion) {
  const lienzo = crear("div", "lienzo-mapa");
  const controles = crear("div", "controles-mapa");
  const pie = crear("div", "pie", "Cargando el mapa…");
  caja.append(crear("h3", "", v.titulo || "Mapa"), controles, lienzo);
  const leyenda = crear("div", "leyenda");
  for (const [c, t] of [["nivel-muy-alto", "Riesgo muy alto"], ["nivel-alto", "Riesgo alto"], ["nivel-otro", "Otra laguna"]]) {
    const s = crear("span");
    s.append(crear("i", `marca ${c}`), document.createTextNode(t));
    leyenda.append(s);
  }
  caja.append(leyenda, pie);
  let gl;
  try {
    gl = await cargarVisorMapa();
  } catch (e) {
    pie.textContent = e.message;
    return;
  }
  const puntos = v.puntos || [];
  const un_punto = puntos.length === 1;
  let mapa = null;
  let enFormacion = false;
  let tres = Boolean(v.tres_d);

  const encuadrar = () => {
    if (puntos.length > 1) {
      const b = new gl.LngLatBounds();
      puntos.forEach((p) => b.extend([p.lon, p.lat]));
      mapa.fitBounds(b, { padding: 50, maxZoom: 11, duration: 0 });
    }
  };

  // Activar el relieve sobre un mapa ya abierto lo deja en blanco: al cambiar de vista se vuelve a crear el mapa en el mismo lugar.
  const montar = (camara) => {
    if (mapa) mapa.remove();
    pie.dataset.fallo = "";
    mapa = new gl.Map({
      container: lienzo,
      attributionControl: { compact: true, customAttribution: "Imagen: Esri, Maxar, Earthstar | Relieve: Terrarium (Mapzen)" },
      pitch: tres ? 62 : 0,
      bearing: tres ? 25 : 0,
      maxPitch: 80,
      center: camara ? camara.center : (puntos.length ? [puntos[0].lon, puntos[0].lat] : [-75.5, -11]),
      zoom: tres ? (camara ? Math.max(camara.zoom, un_punto ? 12.5 : 8) : (un_punto ? 12.5 : 8)) : (camara ? camara.zoom : (un_punto ? 11 : 5)),
      style: {
        version: 8,
        sources: {
          satelite: { type: "raster", tiles: [SATELITE], tileSize: 256, maxzoom: 17 },
          relieve: { type: "raster-dem", tiles: [RELIEVE], tileSize: 256, encoding: "terrarium", maxzoom: 14 },
        },
        layers: [{ id: "fondo", type: "background", paint: { "background-color": "#1b2a33" } }, { id: "satelite", type: "raster", source: "satelite" }],
        ...(tres ? { terrain: { source: "relieve", exaggeration: 1.3 } } : {}),
      },
    });
    mapa.addControl(new gl.NavigationControl({ visualizePitch: true }));
    for (const p of puntos) {
      const el = crear("button", `marca boton ${claseNivel(p.nivel)}`);
      el.type = "button";
      el.setAttribute("aria-label", p.nombre);
      new gl.Marker({ element: el }).setLngLat([p.lon, p.lat]).setPopup(new gl.Popup({ offset: 10 }).setDOMContent(popupLaguna(p))).addTo(mapa);
    }
    if (enFormacion) marcarFormacion();
    mapa.on("error", (e) => {
      if (e && e.error && !pie.dataset.fallo) {
        pie.dataset.fallo = "1";
        pie.textContent = "Algunas imágenes del mapa no cargaron; revisa tu conexión.";
      }
    });
    mapa.on("load", () => {
      if (!pie.dataset.fallo) {
        const cuantas = un_punto ? "1 laguna" : `${puntos.length} lagunas`;
        pie.textContent = `${cuantas} con ubicación tomada de los documentos y validada con el relieve. Haz clic en un punto para ver su fuente.`;
        if (v.sin_ubicacion && v.sin_ubicacion.length) pie.textContent += ` Sin ubicación verificada: ${v.sin_ubicacion.join(", ")}.`;
        if (tres) pie.textContent += " Es el relieve real, con la altura exagerada 1.3 veces; no es una simulación de desborde.";
      }
      if (!camara && !tres) encuadrar();
    });
    b3d.setAttribute("aria-pressed", String(tres));
    b3d.textContent = tres ? "Ver en 2D" : "Ver en 3D";
  };

  const marcarFormacion = () => {
    for (const f of formacion) {
      const el = crear("button", "marca boton nivel-formacion");
      el.type = "button";
      el.setAttribute("aria-label", `Laguna en formación, cordillera ${f.cordillera}`);
      const d = crear("div", "popup-mapa");
      d.append(crear("strong", "", "Laguna en formación"), crear("div", "", `Cordillera ${f.cordillera} · ${f.departamento}`),
        crear("div", "", `${f.tipo}, ${f.altitud} m s. n. m.`), crear("div", "", `INAIGEM 2025, p. ${f.pagina}`));
      new gl.Marker({ element: el }).setLngLat([f.lon, f.lat]).setPopup(new gl.Popup({ offset: 8 }).setDOMContent(d)).addTo(mapa);
    }
  };

  const b3d = crear("button", "", "Ver en 3D");
  b3d.type = "button";
  b3d.addEventListener("click", () => {
    const c = mapa.getCenter();
    tres = !tres;
    montar({ center: [c.lng, c.lat], zoom: mapa.getZoom() });
  });
  controles.append(b3d);
  if (puntos.length > 1) {
    const b = crear("button", "", "Encuadrar todo");
    b.type = "button";
    b.addEventListener("click", encuadrar);
    controles.append(b);
  }
  if (formacion && formacion.length) {
    const b = crear("button", "", `Lagunas en formación (${formacion.length})`);
    b.type = "button";
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => {
      enFormacion = !enFormacion;
      b.setAttribute("aria-pressed", String(enFormacion));
      const c = mapa.getCenter();
      montar({ center: [c.lng, c.lat], zoom: mapa.getZoom() });
    });
    controles.append(b);
    const s = crear("span");
    s.append(crear("i", "marca nivel-formacion"), document.createTextNode("En formación"));
    leyenda.append(s);
  }
  montar(null);
  new ResizeObserver(() => mapa && mapa.resize()).observe(lienzo);
}

const NS = "http://www.w3.org/2000/svg";
function svg(etiqueta, attrs, texto) {
  const e = document.createElementNS(NS, etiqueta);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (texto !== undefined) e.textContent = texto;
  return e;
}
const formato = (n) => (Math.abs(n) >= 1000 ? String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ") : String(Math.round(n * 100) / 100));

function dibujarBarras(caja, v) {
  const alto = 26, izq = 140, ancho = 620, pad = 8;
  const max = Math.max(...v.valores, 1);
  const g = svg("svg", { class: "grafico", viewBox: `0 0 ${ancho + 40} ${v.valores.length * alto + pad * 2}`, role: "img", "aria-label": v.titulo });
  v.valores.forEach((x, i) => {
    const y = pad + i * alto;
    const w = Math.max(2, (x / max) * (ancho - izq - 90));
    g.append(svg("text", { x: izq - 8, y: y + 15, "text-anchor": "end" }, String(v.etiquetas[i]).slice(0, 20)));
    const r = svg("rect", { class: "barra", x: izq, y: y + 3, width: w, height: alto - 9, rx: 3 });
    r.append(svg("title", {}, `${v.etiquetas[i]}: ${formato(x)} ${v.unidad || ""}`));
    g.append(r, svg("text", { x: izq + w + 6, y: y + 15, class: "suave" }, `${formato(x)} ${v.unidad || ""}`));
  });
  caja.append(crear("h3", "", v.titulo), g, crear("div", "pie", v.fuente || ""));
}

function dibujarLinea(caja, v) {
  const W = 660, H = 280, iz = 62, de = 22, ar = 18, ab = 34;
  const min = Math.min(...v.valores), max = Math.max(...v.valores);
  const margen = (max - min) * 0.12 || max * 0.1 || 1;
  const a = min - margen, b = max + margen;
  const X = (i) => iz + (v.valores.length > 1 ? (i / (v.valores.length - 1)) * (W - iz - de) : 0);
  const Y = (x) => ar + (1 - (x - a) / (b - a)) * (H - ar - ab);
  const g = svg("svg", { class: "grafico", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": v.titulo });
  for (let i = 0; i <= 4; i++) {
    const x = a + (i / 4) * (b - a);
    g.append(svg("line", { class: "eje", x1: iz, x2: W - de, y1: Y(x), y2: Y(x) }), svg("text", { x: iz - 6, y: Y(x) + 4, "text-anchor": "end", class: "suave" }, formato(x)));
  }
  g.append(svg("polyline", { class: "serie", points: v.valores.map((x, i) => `${X(i)},${Y(x)}`).join(" ") }));
  v.valores.forEach((x, i) => {
    const c = svg("circle", { class: "punto-serie", cx: X(i), cy: Y(x), r: 4 });
    c.append(svg("title", {}, `${v.etiquetas[i]}: ${formato(x)} ${v.unidad || ""}`));
    g.append(c);
    if (v.valores.length <= 12 || i % Math.ceil(v.valores.length / 10) === 0) g.append(svg("text", { x: X(i), y: H - 12, "text-anchor": "middle", class: "suave" }, String(v.etiquetas[i])));
  });
  caja.append(crear("h3", "", `${v.titulo} (${v.unidad || ""})`), g, crear("div", "pie", v.fuente || ""));
}

const COLOR_TIPO = { Laguna: "#2f8fce", Glaciar: "#67b7d1", Nevado: "#8fa6b2", CentroPoblado: "#e0693e", Evento: "#d0342c", Institucion: "#ef8a17",
  ObraSeguridad: "#b26a00", MetodoMonitoreo: "#7c5cd6", NivelRiesgo: "#9d0208", Cuenca: "#3e9c6a", Cordillera: "#6c757d" };

function dibujarSubgrafo(caja, v) {
  const W = 860, H = 500, cx = W / 2, cy = H / 2;
  const g = svg("svg", { class: "grafico", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Conexiones de ${v.centro}` });
  // una entidad puede tener varias relaciones con el centro: se dibuja un solo punto
  const unicos = new Map();
  for (const a of v.aristas) {
    const k = `${a.a}|${a.tipo}`;
    if (!unicos.has(k)) unicos.set(k, { ...a, detalle: [] });
    unicos.get(k).detalle.push(`${a.sale ? v.centro + " → " + a.relacion + " → " + a.a : a.a + " → " + a.relacion + " → " + v.centro}${a.fuente ? " (" + a.fuente + ")" : ""}`);
  }
  const lista = [...unicos.values()];
  const n = lista.length;
  const nodos = lista.map((a, i) => {
    const ang = (i / n) * Math.PI * 2 - Math.PI / 2;
    const r = i % 2 ? 1 : 0.8;   // dos anillos alternados para que no se monten las etiquetas
    return { ...a, x: cx + Math.cos(ang) * 240 * r, y: cy + Math.sin(ang) * 195 * r, ang };
  });
  nodos.forEach((a) => g.append(svg("line", { class: "arista", x1: cx, y1: cy, x2: a.x, y2: a.y })));
  nodos.forEach((a) => {
    const c = svg("circle", { class: "nodo", cx: a.x, cy: a.y, r: 7, fill: COLOR_TIPO[a.tipo] || "#8fa6b2" });
    c.append(svg("title", {}, a.detalle.join("\n")));
    const derecha = Math.cos(a.ang) >= 0;
    const nombre = String(a.a);
    const e = svg("text", { x: a.x + (derecha ? 11 : -11), y: a.y + 4, "text-anchor": derecha ? "start" : "end" }, nombre.length > 24 ? nombre.slice(0, 23) + "…" : nombre);
    e.append(svg("title", {}, nombre));
    g.append(c, e);
  });
  g.append(svg("circle", { class: "nodo-centro", cx, cy, r: 13 }), svg("text", { x: cx, y: cy - 19, "text-anchor": "middle" }, v.centro));
  const tipos = [...new Set(lista.map((a) => a.tipo))];
  const leyenda = crear("div", "leyenda");
  for (const t of tipos) {
    const s = crear("span");
    const m = crear("i", "marca");
    m.style.background = COLOR_TIPO[t] || "#8fa6b2";
    s.append(m, document.createTextNode(t));
    leyenda.append(s);
  }
  caja.append(crear("h3", "", `Conexiones de ${v.centro} en el grafo`), g, leyenda, crear("div", "pie", "Pasa el cursor sobre un punto para ver la relación y su documento."));
}

function dibujarFicha(caja, v) {
  const t = crear("h3", "", `Ficha de ${v.nombre}`);
  if (v.nivel) {
    const i = crear("span", `insignia ${claseNivel(v.nivel)}`, `Riesgo ${v.nivel.toLowerCase()}`);
    t.append(i);
  }
  caja.append(t);
  const datos = crear("div", "ficha-datos");
  const filas = [["Ubicación", v.departamentos], ["Altitud", v.altitud], ["Volumen (valores citados)", v.volumen], ["Poblados expuestos", v.poblados], ["Glaciares que la alimentan", v.glaciares],
    ["Obras de seguridad", v.obras], ["Estado de las obras", v.estados], ["Eventos", v.eventos], ["Monitoreo", v.monitoreo]];
  for (const [nombre, valores] of filas) {
    if (!valores || !valores.length) continue;
    const d = crear("div");
    d.append(crear("b", "", nombre), document.createTextNode(valores.join(", ")));
    datos.append(d);
  }
  caja.append(datos);
  if (!v.nivel && v.entidad === "Laguna") caja.append(crear("div", "pie", "El grafo no registra su nivel de riesgo; revisa la respuesta y sus fuentes."));
  if (v.punto) {
    const b = crear("button", "boton-sec", "Ver en 3D");
    b.type = "button";
    b.addEventListener("click", () => {
      b.remove();
      const m = crear("div", "visual");
      caja.after(m);
      dibujarMapa(m, { titulo: `${v.nombre} sobre el relieve`, puntos: [v.punto], tres_d: true, sin_ubicacion: [] });
    });
    caja.append(b);
  }
}

function dibujarVisual(padre, v) {
  padre.classList.add("con-visual");
  const caja = crear("div", "visual");
  padre.append(caja);
  if (v.tipo === "mapa") dibujarMapa(caja, v);
  else if (v.tipo === "barras") dibujarBarras(caja, v);
  else if (v.tipo === "linea") dibujarLinea(caja, v);
  else if (v.tipo === "subgrafo") dibujarSubgrafo(caja, v);
  else if (v.tipo === "ficha") dibujarFicha(caja, v);
}

async function abrirMapaGeneral() {
  const m = agregarMensaje("agente", "");
  m.classList.add("con-visual");
  try {
    const r = await fetch("/api/mapa");
    if (!r.ok) throw new Error("El mapa aún no está disponible; intenta en un momento.");
    const d = await r.json();
    const caja = crear("div", "visual");
    m.append(caja);
    await dibujarMapa(caja, { titulo: "Lagunas glaciares del Perú", puntos: d.lagunas, sin_ubicacion: [], tres_d: false }, d.formacion);
    m.scrollIntoView({ block: "end" });
  } catch (e) {
    m.append(crear("div", "aviso", e.message));
  }
}
$("abrir-mapa").addEventListener("click", abrirMapaGeneral);

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
