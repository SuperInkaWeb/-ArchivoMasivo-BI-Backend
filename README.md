# datafilter-backend

Backend para **subir archivos grandes (CSV/TXT/Excel), filtrarlos y descargar solo el resultado filtrado**, diseñado para escalar a **millones de filas** por archivo.

## Enfoque técnico

- **Motor:** DuckDB + Parquet columnar. Al subir, cada archivo se convierte a Parquet (comprimido, columnar). Filtrar sobre millones de filas responde en milisegundos leyendo solo las columnas necesarias.
- **Memoria constante:** ingesta y descarga se hacen en *streaming* vía `COPY` de DuckDB — no se carga el archivo entero en RAM.
- **Seguridad:** los filtros del usuario nunca se concatenan como SQL. Las columnas se validan contra el esquema real (whitelist), los operadores son un enum cerrado, y los valores van siempre por *binding* parametrizado.

> ⚠️ **Excel** tiene un tope de **1.048.576 filas por hoja**. Los "millones de líneas" solo son viables en **CSV/TXT**. Excel se soporta para archivos pequeños/medianos y como formato de descarga cuando el resultado cabe.

## Arquitectura (Clean Architecture)

```
app/
├── routers/       Reciben la petición HTTP y delegan (upload, filter, download)
├── services/      Lógica de negocio (ingesta, orquestación, exportación)
├── repositories/  Acceso a datos (DuckDB sobre Parquet + metadatos SQLite)
├── schemas/       DTOs Pydantic (validación entrada/salida)
└── core/          Config, storage, seguridad (query-builder), rate limit, errores
```

## Puesta en marcha

```bash
# 1. Crear entorno (Python 3.11 recomendado)
py -3.11 -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 2. Configurar entorno
copy .env.example .env

# 3. Prueba end-to-end del motor (genera 2M filas, filtra y exporta)
python -m scripts.smoke_test

# 4. Levantar la API
uvicorn app.main:app --reload
```

Docs interactivas: `http://localhost:8000/docs`

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/datasets/upload` | Sube uno o varios archivos (ingesta async) |
| `GET`  | `/datasets` | Lista datasets y su estado |
| `GET`  | `/datasets/{id}` | Detalle + esquema de columnas |
| `DELETE` | `/datasets/{id}` | Elimina dataset y sus archivos |
| `POST` | `/datasets/{id}/preview` | Previsualiza filtrado (paginado) |
| `POST` | `/datasets/{id}/download` | Descarga el resultado filtrado (CSV/XLSX) |

### Ejemplo de filtro (preview / download)

```json
{
  "conditions": [
    { "column": "region", "operator": "eq", "value": "Lima" },
    { "column": "monto",  "operator": "gt", "value": 1000 }
  ],
  "combinator": "and",
  "select": ["id", "region", "monto"],
  "sort": [{ "column": "monto", "direction": "desc" }]
}
```

## Pendiente (siguientes fases)

- Frontend (React + shadcn) con UI de filtros y descarga.
- Autenticación (Auth0) y aislamiento de datasets por usuario.
- Worker/cola real para ingesta (hoy usa BackgroundTasks).
- Timeout duro de consultas y cuota de disco por usuario.
