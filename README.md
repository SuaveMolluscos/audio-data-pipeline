# Audio Data Pipeline

[![CI](https://github.com/SuaveMolluscos/audio-data-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/SuaveMolluscos/audio-data-pipeline/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Pipeline ETL para recopilar, normalizar y conservar metadatos musicales. Combina varias fuentes públicas, una cola persistente en SQLite, análisis opcional de previews de audio y una interfaz de terminal para operar el crawler.

> Proyecto personal y comunitario. Las APIs externas, su disponibilidad y sus condiciones de uso son independientes de este proyecto.

## Funcionalidades

- Cola SQLite durable con deduplicación, prioridades, reintentos limitados y recuperación de trabajos abandonados mediante leases.
- Workers concurrentes y apagado ordenado.
- Enriquecimiento de metadatos con iTunes Search, MusicBrainz, Deezer y Last.fm.
- Identificación con puntuación de candidatos; coincidencias dudosas se guardan para revisión en vez de expandir el crawler.
- Extracción **opcional** de tempo, tonalidad y otras características de audio desde previews, con Librosa y FFmpeg.
- Historial de análisis, procedencia de los datos, exportación CSV y copia consistente de la base SQLite.
- CLI interactiva, monitor operativo y ejemplos de despliegue en Debian/systemd.

## Requisitos

- Python 3.10 o posterior.
- Acceso a Internet para consultar las APIs.
- Las funciones de metadatos y cola usan la biblioteca estándar de Python.
- Para analizar audio se necesitan las dependencias del extra `audio`. Last.fm requiere una clave API; sin ella, el crawler continúa sin etiquetas de esa fuente.

## Instalación

Desde la raíz del repositorio:

```bash
python -m venv .venv
```

Activa el entorno virtual y luego instala el proyecto:

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

Para habilitar la extracción de características de audio:

```bash
python -m pip install -e ".[audio]"
```

En Linux, si FFmpeg no está disponible a través de `imageio-ffmpeg`, instálalo con el gestor de paquetes de la distribución. Si no instalas el extra de audio, el pipeline sigue funcionando con metadatos y registra que el análisis de audio no está disponible.

## Inicio rápido

Analiza una canción directamente:

```bash
audio-analyzer "Billie Jean | Michael Jackson"
```

Encola semillas y procesa la cola:

```bash
audio-seeds --db data/tracks.db enqueue --categories pop rock
audio-crawler --db data/tracks.db --workers 2
```

También puedes pasar semillas al crawler sin usar el comando de semillas:

```bash
audio-crawler --db data/tracks.db \
  --seed "Billie Jean|Michael Jackson" \
  --seed "Take Five|Dave Brubeck" \
  --workers 2
```

Abre el centro de control o el monitor:

```bash
audio-pipeline
audio-monitor --db data/tracks.db
audio-monitor --db data/tracks.db --summary --no-color
```

Los comandos también pueden ejecutarse sin instalar los entry points:

```bash
python analyzer.py "Billie Jean | Michael Jackson"
python seeds.py --db data/tracks.db enqueue --categories pop rock
python crawler.py --db data/tracks.db --workers 2
python main.py
python monitor.py --db data/tracks.db --summary --no-color
```

El directorio `data/` se crea como ejemplo; la base de datos no se distribuye ni se versiona.

## Configuración

La configuración se puede pasar por argumentos o variables de entorno. El crawler acepta `CRAWLER_WORKERS` y `CRAWLER_LEASE_SECONDS`. El analizador admite:

| Variable | Uso |
|---|---|
| `LASTFM_API_KEY` | Habilita la consulta de etiquetas de Last.fm. |
| `MUSIC_ANALYZER_USER_AGENT` | Identifica el cliente en las solicitudes HTTP; configura un contacto válido al desplegar. |
| `ANALYZER_LOG_FILE` | Ruta del log del analizador. |

Ejemplo en PowerShell:

```powershell
$env:LASTFM_API_KEY = "tu_clave"
$env:MUSIC_ANALYZER_USER_AGENT = "MiCrawler/1.0 (contacto: correo@example.com)"
```

No agregues claves API, bases de datos, exports ni logs al repositorio. Usa archivos de entorno locales o el gestor de secretos de tu plataforma de despliegue.

## Estructura

| Archivo/directorio | Responsabilidad |
|---|---|
| `analyzer.py` | Consultas a fuentes externas, resolución de identidad y análisis opcional de audio. |
| `crawler.py` | Workers, ejecución de la cola, expansión y manejo de reintentos. |
| `db.py` | Esquema, migraciones y persistencia SQLite. |
| `seeds.py` | Colección inicial por categorías y comandos para encolar semillas. |
| `main.py` | Centro de control interactivo local. |
| `monitor.py` | Monitor, consultas operativas y exportación. |
| `tests/` | Pruebas automatizadas con `unittest`. |
| `deploy/` | Plantillas systemd y scripts de operación para Debian. |

## Datos y límites

- Los resultados dependen de la cobertura y disponibilidad de proveedores externos; una búsqueda sin coincidencias no implica que una grabación no exista.
- Si no se proporciona artista, una coincidencia basada solo en el título queda marcada para revisión.
- Las características de audio se calculan a partir de previews breves: no representan necesariamente la grabación completa y pueden no estar disponibles.
- Las solicitudes se limitan y reintentan para reducir la presión sobre servicios externos. Respeta los términos, políticas y límites de cada proveedor.
- Verifica derechos y condiciones antes de redistribuir datos, previews o exports obtenidos de terceros.

## Pruebas y calidad

```bash
python -m unittest discover -s tests -v
python -m compileall -q analyzer.py crawler.py db.py main.py monitor.py seeds.py
```

Para instalar las herramientas de desarrollo y ejecutar Ruff:

```bash
python -m pip install -e ".[dev]"
ruff check .
```

GitHub Actions ejecuta estas comprobaciones en Python 3.10–3.14. La matriz valida el pipeline base; las dependencias del extra opcional `audio` no se instalan en ese job.

## Despliegue

Las instrucciones para desplegar el servicio de larga duración y sus backups en Debian están en [`deploy/README.md`](deploy/README.md). Revísalas y adapta usuarios, rutas, límites de recursos y configuración antes de habilitar systemd. Prueba la restauración de una copia de seguridad antes de depender del servicio.

## Licencia

Distribuido bajo la licencia MIT. Consulta [`LICENSE`](LICENSE).
