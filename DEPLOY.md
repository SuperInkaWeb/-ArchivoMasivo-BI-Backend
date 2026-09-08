# Despliegue — DataFilter

Arquitectura de producción:

```
Vercel (frontend React)  ──HTTPS──►  Railway (FastAPI + DuckDB)  ──►  Neon (Postgres: metadatos)
                                              └─ archivos + Parquet en el disco de Railway
```

> ⚠️ **Persistencia de archivos (pendiente):** por ahora los archivos subidos y los `.parquet`
> viven en el disco de Railway, que es **efímero**: se borran en cada redeploy o reinicio.
> Los **metadatos** (en Neon) sí persisten, así que tras un redeploy los datasets aparecerán
> pero sin sus archivos (filtrar/descargar fallará hasta re-subirlos). Para producción real
> hay que añadir un **Volumen de Railway** o **R2/S3** — es el siguiente paso.

**Prerrequisito:** ambos repos (`datafilter-backend` y `datafilter-frontend`) subidos a GitHub.

---

## 1. Neon (base de datos de metadatos)

1. Entra a [neon.tech](https://neon.tech) → **New Project**.
2. Nómbralo `datafilter` y elige la región más cercana.
3. En **Connection Details** copia la **Connection string** (formato):
   ```
   postgresql://usuario:password@ep-xxxx.region.aws.neon.tech/neondb?sslmode=require
   ```
   Ese valor es tu `DATABASE_URL`. (Las tablas se crean solas al arrancar el backend.)

---

## 2. Railway (backend)

1. En [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo** → elige `datafilter-backend`.
2. Railway detecta el `Dockerfile` y `railway.json ` (healthcheck en `/health`).
3. En **Variables**, agrega:

   | Variable | Valor |
   |---|---|
   | `DATABASE_URL` | la connection string de Neon (paso 1) |
   | `AUTH_ENABLED` | `true` |
   | `AUTH0_DOMAIN` | `tu-tenant.us.auth0.com` |
   | `AUTH0_AUDIENCE` | `https://datafilter-api` |
   | `CORS_ORIGINS` | *(se completa en el paso 4, con la URL de Vercel)* |
   | `MAX_UPLOAD_MB` | `2048` (ajústalo a tu plan) |

   > No definas `PORT` — Railway lo inyecta solo.
4. Deploy. Cuando termine, en **Settings → Networking → Generate Domain** obtienes la URL pública,
   p. ej. `https://datafilter-backend-production.up.railway.app`. Guárdala.
5. Verifica: abre `https://TU-BACKEND.up.railway.app/health` → debe responder `{"status":"ok"}`.

---

## 3. Vercel (frontend)

1. En [vercel.com](https://vercel.com) → **Add New → Project** → importa `datafilter-frontend`.
2. Framework: **Vite** (autodetectado; `vercel.json` ya fija build y salida `dist`).
3. En **Environment Variables** agrega:

   | Variable | Valor |
   |---|---|
   | `VITE_API_URL` | la URL de Railway (paso 2.4) |
   | `VITE_AUTH_ENABLED` | `true` |
   | `VITE_AUTH0_DOMAIN` | `tu-tenant.us.auth0.com` |
   | `VITE_AUTH0_CLIENT_ID` | Client ID de tu SPA de Auth0 |
   | `VITE_AUTH0_AUDIENCE` | `https://datafilter-api` |
4. **Deploy**. Obtendrás una URL, p. ej. `https://datafilter.vercel.app`. Guárdala.

---

## 4. Cerrar el círculo (CORS + Auth0)

1. **Railway → Variables:** pon `CORS_ORIGINS` = la URL de Vercel (`https://datafilter.vercel.app`).
   Railway redepliega automáticamente.
2. **Auth0 → tu SPA → Settings → Application URIs:** agrega la URL de Vercel a las tres listas
   (separadas por coma junto a `http://localhost:5173`):
   - Allowed Callback URLs
   - Allowed Logout URLs
   - Allowed Web Origins

---

## 5. Probar

Abre la URL de Vercel → **Iniciar sesión** → entras con tu usuario de Auth0 → sube y filtra.

---

## Referencia rápida de variables

**Backend (Railway):** `DATABASE_URL`, `AUTH_ENABLED`, `AUTH0_DOMAIN`, `AUTH0_AUDIENCE`, `CORS_ORIGINS`, `MAX_UPLOAD_MB`, `MAX_DOWNLOAD_ROWS`.

**Frontend (Vercel):** `VITE_API_URL`, `VITE_AUTH_ENABLED`, `VITE_AUTH0_DOMAIN`, `VITE_AUTH0_CLIENT_ID`, `VITE_AUTH0_AUDIENCE`.

El `AUTH0_AUDIENCE` (backend) y `VITE_AUTH0_AUDIENCE` (frontend) deben ser **idénticos**, igual que el dominio de Auth0.
