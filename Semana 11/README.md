# Agente con herramientas · Glaciares (semana 11)

**Curso:** Gestión del Conocimiento (UNMSM, FISI) · 2026-II
**Grupo:** G8 · Enterprise Brain Glaciares
**Agente en la nube:** <https://agente-glaciares.jollyplant-0c13b6a7.chilecentral.azurecontainerapps.io>

El GraphRAG de la semana 10 elige una herramienta y responde. Este agente trabaja en un ciclo: Groq o Gemini interpretan la
pregunta, eligen una herramienta, leen el resultado y deciden si necesitan otra. Antes de mostrar la respuesta se verifica que
sus citas y cifras estén en la evidencia. Cada dato conserva su documento y página.

Esta entrega no modifica las semanas anteriores: solo lee sus datos.

## Cómo está organizado

```
semana11_agente_glaciares.ipynb    Demo: conversa con el agente en la nube (ya ejecutado)
agente_glaciares/
  agente.py                        El ciclo del agente (LangGraph)
  herramientas.py                  Las 7 herramientas y la numeración de las citas
  llm.py                           Cadena de modelos: si uno falla o agota su cuota, pasa al siguiente
  grafo_resiliente.py              Neo4j y, si no responde, una copia del grafo en Kùzu
  rag_ligero.py                    Búsqueda en los documentos con los embeddings de la semana 9
  graphrag_glaciares.py            Motor de la semana 10 (copia sin cambios)
  servicio.py, api.py, config.py   Servicio, API web y configuración
  static/                          Página de chat
  evaluar_agente.py                Evaluación y archivo de entrada para RAGAS
  banco_preguntas.json             Las 18 preguntas de la semana 10
  resultados/                      Evaluación de las 18 preguntas contra el servicio en la nube
  scripts/                         Preparar datos, comprobar claves, recargar Neo4j y desplegar
  tests/                           Pruebas automáticas
  requirements.txt                 Dependencias completas
  requirements-ligero.txt          Dependencias para probarlo sin el modelo de embeddings
  Dockerfile, .env.example         Imagen del servicio y plantilla de claves
```

Fuera de esta carpeta solo se agrega `.github/workflows/agente-imagen.yml`, que construye la imagen del servicio en GitHub.

## Herramientas del agente

| Herramienta | Qué hace |
|---|---|
| `consulta_verificada` | Ejecuta las consultas Cypher probadas: P1–P10 y E1–E12 |
| `ficha_entidad` | Devuelve lo que el grafo sabe de una laguna, glaciar o poblado, con documento y página |
| `camino_entidades` | Encuentra la cadena de relaciones que conecta dos entidades |
| `consulta_libre` | Escribe una consulta Cypher nueva; se valida y solo puede leer |
| `buscar_documentos` | Busca en los 398 documentos, con filtros por institución y año |
| `comparar_superficie` | Calcula el cambio de superficie glaciar entre inventarios |
| `buscar_entidad` | Encuentra un nombre mal escrito en el catálogo del grafo |

## Cómo probarlo

Elige una opción. Las tres responden con el mismo agente; cambia lo que necesitas instalar.

| Opción | Necesitas | Tiempo aproximado |
|---|---|---|
| 1. En la nube | Solo un navegador | 1 minuto |
| 2. En tu computador, modo ligero | Python 3.12, 2 GB libres y una clave de Groq | 10 minutos |
| 3. En tu computador, modo completo | Python 3.12, 8 GB libres y una clave de Groq | 30 minutos |

### Opción 1. En la nube

1. Abre la [página del agente](https://agente-glaciares.jollyplant-0c13b6a7.chilecentral.azurecontainerapps.io). Si estuvo sin uso, tarda unos 30 segundos en despertar; el indicador de arriba a la derecha muestra cuándo está listo.
2. Escribe una pregunta de la tabla de la sección siguiente.
3. A la derecha, en "Cómo respondió", verás cada herramienta que usó. Debajo, las fuentes: haz clic en un número de cita para ubicar su fuente y en el título para abrir la ficha del documento.

Para usar la API, consulta su [documentación interactiva](https://agente-glaciares.jollyplant-0c13b6a7.chilecentral.azurecontainerapps.io/docs). También puedes abrir `semana11_agente_glaciares.ipynb` en Google Colab y ejecutarlo: no necesita claves.

### Opción 2. En tu computador, modo ligero

Busca en los documentos solo por palabras clave (BM25), sin el modelo de embeddings. Todo lo demás funciona igual. Los comandos son para PowerShell en Windows.

**Paso 1. Descarga el proyecto.**

```powershell
git clone -b entrega-semana-11-agente https://github.com/LuisMendozaAymara/BrainGlaciares.git
cd "BrainGlaciares\Semana 11\agente_glaciares"
```

Cuando la entrega esté incorporada al repositorio del grupo, usa `git clone https://github.com/EdsonPerez7/BrainGlaciares.git`.

**Paso 2. Crea el entorno e instala las dependencias.**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-ligero.txt
```

**Paso 3. Copia los datos de las semanas 7 y 9.**

```powershell
.venv\Scripts\python scripts\preparar_datos.py
```

**Paso 4. Escribe tus claves.**

```powershell
Copy-Item .env.example .env
notepad .env
```

Completa estas líneas sin comillas y guarda el archivo:

```
GROQ_API_KEY=tu_clave
EMBEDDER=ninguno
```

La clave de Groq es gratuita y se crea en <https://console.groq.com/keys>. Con una clave de Gemini de <https://aistudio.google.com/apikey> en `GOOGLE_API_KEY` tendrás un respaldo si se agota la cuota de Groq. Para usar el grafo del grupo, agrega también `NEO4J_URI` y `NEO4J_PASSWORD`, y `NEO4J_USERNAME` si tu usuario no es `neo4j`; son los datos de los Secrets de Colab. Git ignora el archivo `.env`: no compartas su contenido.

**Paso 5. Si no usarás Neo4j, prepara la copia del grafo.** Solo se hace una vez y tarda unos minutos.

```powershell
.venv\Scripts\python -c "from grafo_resiliente import construir_base_previa as c; c('datos')"
```

**Paso 6. Comprueba tus claves.**

```powershell
.venv\Scripts\python scripts\comprobar_entorno.py
```

Al menos un modelo debe aparecer con `OK`. Es normal que alguno falle por saturación o porque ya no exista: el agente usa los demás. Si Neo4j está configurado, debe mostrar su número de nodos y relaciones.

**Paso 7. Inicia el agente.**

```powershell
.venv\Scripts\python -m uvicorn api:app --port 8080
```

Abre <http://localhost:8080> y espera a que el indicador quede en verde. Para detenerlo, presiona `Ctrl+C` en la terminal.

### Opción 3. En tu computador, modo completo

Agrega la búsqueda semántica con el modelo bge-m3, igual que en la nube. Sigue los pasos de la opción 2 con estos cambios:

- En el paso 2, instala las dependencias completas:

  ```powershell
  .venv\Scripts\python -m pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
  .venv\Scripts\python -m pip install -r requirements.txt
  ```

- En el paso 4, no agregues la línea `EMBEDDER=ninguno`.
- En el paso 7, la primera vez se descargan 2.3 GB del modelo, así que el indicador tardará más en quedar en verde.

Si prefieres Docker, reserva 15 GB, copia los datos con el paso 3 y escribe tus claves con el paso 4. Luego:

```powershell
docker build -t agente-glaciares .
docker run --rm --env-file .env -p 8080:8080 agente-glaciares
```

## Preguntas para probar

| Pregunta | Qué debe ocurrir |
|---|---|
| ¿Qué lagunas de Áncash tienen riesgo muy alto de desborde? | Lista las lagunas con sus citas |
| las lagunas mas peligrosas de cusco | Responde A10 y Upiscocha: entiende minúsculas y que "peligrosa" significa riesgo |
| ¿Por qué la laguna Palcacocha es peligrosa? | Explica las causas y cita sus fuentes |
| ¿Cómo se relaciona el nevado Huascarán con Yungay? | Muestra el camino de relaciones en el grafo |
| ¿Cómo cambió la superficie glaciar de la Cordillera Blanca entre 2018 y 2023? | Calcula el cambio: de 449.75 a 424.86 km² |
| ¿Qué glaciares alimentan Palcacocha? y luego ¿Y qué obras de seguridad tiene esa laguna? | Entiende la segunda pregunta por la conversación |
| ¿Quién ganó el Mundial de fútbol 2022? | No inventa: dice que no encontró la información |

El LLM redacta cada vez de forma distinta, así que el texto puede variar entre intentos.

## Resultados

Evaluación de las 18 preguntas contra el servicio en la nube, con Groq, Gemini y Neo4j. El detalle está en `agente_glaciares/resultados/`.

| Indicador | Valor |
|---|---|
| Preguntas de competencia respondidas con citas | 10 / 10 |
| Preguntas resueltas por el agente con LLM | 17 / 18 (la del Mundial se rechaza sin llamar al LLM) |
| Tiempo promedio por pregunta | 4.1 s |
| Avisos de la verificación | 0 |
| Pruebas automáticas | 153: 131 con LLM simulado y 22 contra un Neo4j local |

Pruebas de funcionamiento:

| Situación | Resultado |
|---|---|
| Se apaga Neo4j con el servicio en marcha | Responde con la copia en Kùzu; la primera consulta tarda unos 6 s y luego regresa a Neo4j solo |
| Groq agota su cuota a mitad de una pregunta | Gemini continúa la misma pregunta |
| Ningún LLM responde | Muestra la evidencia encontrada, con su fuente, sin redactar |
| El servicio queda sin uso | Se apaga tras unos 6 minutos y no consume crédito; despierta en 26 s |
| Un LLM escribe una consulta que modifica el grafo | Se rechaza y la sesión de Neo4j es de solo lectura |
| Una pregunta contiene instrucciones para revelar datos internos | No las obedece |

## Limitaciones conocidas

- **La verificación revisa citas y cifras, no el sentido de cada frase.** Eso lo mide RAGAS en la evaluación integral, con `resultados/ragas_semana11.jsonl`, que tiene el mismo formato de la semana 10.
- **El grafo de Aura y la copia de la semana 7 no coinciden.** Aura tiene 13 638 nodos y 35 374 relaciones, y el CSV 13 829 y 36 596. En 11 de las 18 consultas verificadas los resultados cambian, así que una respuesta puede variar si Aura se pausa y el agente pasa a la copia.
- **En Aura, la laguna Llaca no tiene nivel de riesgo**, aunque los documentos la catalogan como de riesgo muy alto. Por eso P2 devuelve 4 lagunas y no 5. El grupo debe confirmar si esa curaduría fue intencional.
- **El grafo y los documentos pueden diferir.** En algunos departamentos el grafo no registra lagunas en riesgo y el agente responde con los documentos, por ejemplo en Arequipa.
- **Cuota gratuita de Groq:** 8 000 tokens por minuto, y cada llamada usa de 3 000 a 5 500. Con varias preguntas seguidas el agente pasa a Gemini o espera unos segundos.

## Evaluar el agente

```powershell
py -3.12 evaluar_agente.py --url https://agente-glaciares.jollyplant-0c13b6a7.chilecentral.azurecontainerapps.io --salida resultados --pausa 20
```

Genera `respuestas_agente.csv`, `reporte_agente.md` y `ragas_semana11.jsonl`. Para evaluar otro banco, agrega `--banco mi_banco.json`, un archivo con una lista de `{"id", "tipo", "pregunta"}`.

## Publicar una versión nueva en Azure

El servicio corre en Azure Container Apps, con la suscripción Azure for Students. Necesitas [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli-windows) y acceso a esa suscripción.

1. Sube tus cambios a la rama. GitHub Actions ejecuta las pruebas y publica la imagen; el resumen de la ejecución indica su etiqueta.
2. Ejecuta el script con esa etiqueta. Lee las claves de `.env` y las guarda como secretos de Azure:

   ```powershell
   az login
   .\scripts\desplegar_azure.ps1 -Imagen ghcr.io/luismendozaaymara/brainglaciares-agente:ETIQUETA
   ```

Usa `chilecentral`, la región por defecto; la suscripción de la UNMSM también permite `brazilsouth`, `canadacentral`, `westus2` y `northcentralus`. No uses la etiqueta `latest`, porque Azure podría no crear una revisión nueva.

## Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `py` no se reconoce | Python no está en el sistema | Instala Python 3.12 desde <https://www.python.org/downloads/> |
| El indicador dice "sin LLM: modo evidencia" | Faltan las claves o se agotaron las cuotas | Ejecuta `scripts\comprobar_entorno.py` |
| El indicador dice "Kùzu" | Neo4j en pausa o sin configurar | Reanuda la instancia en <https://console.neo4j.io>; el agente regresa solo en 5 minutos |
| El agente tarda varios minutos en iniciar sin Neo4j | Falta la copia del grafo | Ejecuta el paso 5 de la opción 2 |
| El puerto 8080 está ocupado | Otro programa lo usa | Cambia el comando a `--port 8090` y abre esa dirección |
| Error 429 al preguntar | Límite de preguntas por minuto | Espera un momento o agrega `LIMITE_IP_MIN=60` en `.env` |
| La instancia de Aura desapareció | Pasó 30 días en pausa | Crea otra vacía y ejecuta `scripts\cargar_neo4j.py`; carga el CSV de la semana 7 |
