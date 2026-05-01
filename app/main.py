from typing import Any
import logging
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.exceptions import configure_exception_handlers
from app.api.v1.api import api_router
from app.db.connection import get_db_connection
from app.core.logging_config import setup_logging

# Configurar logging
setup_logging()
logger = logging.getLogger(__name__)

def create_application() -> FastAPI:
    """
    Crea y configura la aplicación FastAPI
    """
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=settings.DESCRIPTION,
        root_path="/apivac",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        redirect_slashes=False  # ✅ Deshabilitar redirecciones automáticas
    )

    # CORS: orígenes explícitos y credenciales habilitadas
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Manejadores de excepciones custom
    configure_exception_handlers(app)

    # Rutas API v1
    app.include_router(api_router, prefix=settings.API_V1_STR)
    
    # Inicializar Firebase Admin SDK (opcional, solo si está configurado)
    try:
        import os
        import pathlib
        import json
        from app.services.notificaciones_service import NotificacionesService
        
        # Intentar obtener ruta de credenciales desde variable de entorno
        firebase_cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        firebase_project_id = os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        firebase_cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")  # Variable de entorno con contenido JSON
        
        # Verificar si estamos en producción
        is_production = os.getenv("ENVIRONMENT", "development").lower() == "production"
        
        # Si hay credenciales en variable de entorno (JSON como string), crear archivo temporal
        if firebase_cred_json and not firebase_cred_path:
            try:
                # Parsear JSON para validar
                cred_data = json.loads(firebase_cred_json)
                # Crear archivo temporal
                import tempfile
                temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
                json.dump(cred_data, temp_file)
                temp_file.close()
                firebase_cred_path = temp_file.name
                logger.info("✅ Credenciales de Firebase leídas desde variable de entorno FIREBASE_CREDENTIALS_JSON")
                # Extraer project_id si no está configurado
                if not firebase_project_id and 'project_id' in cred_data:
                    firebase_project_id = cred_data['project_id']
            except json.JSONDecodeError as e:
                logger.error(f"⚠️ Error parseando FIREBASE_CREDENTIALS_JSON: {str(e)}")
            except Exception as e:
                logger.error(f"⚠️ Error creando archivo temporal de credenciales: {str(e)}")
        
        # Si no hay ruta configurada y NO estamos en producción, buscar archivo JSON en la raíz del proyecto
        if (not firebase_cred_path or not os.path.exists(firebase_cred_path)) and not is_production:
            # Calcular la raíz del proyecto: desde app/main.py
            # Subir 1 nivel: app -> raíz
            current_file = pathlib.Path(__file__).resolve()
            project_root = current_file.parent.parent  # app/main.py -> app -> raíz
            
            # Buscar archivos JSON de Firebase con diferentes patrones
            patterns = [
                "*firebase-adminsdk*.json",
                "*firebase*.json",
                "serviceAccountKey.json"
            ]
            
            json_files = []
            for pattern in patterns:
                json_files.extend(list(project_root.glob(pattern)))
                if json_files:
                    break
            
            if json_files:
                firebase_cred_path = str(json_files[0].resolve())
                logger.info(f"✅ Archivo JSON de Firebase encontrado automáticamente: {firebase_cred_path}")
        
        if firebase_cred_path and os.path.exists(firebase_cred_path):
            if NotificacionesService.inicializar_firebase(firebase_cred_path, firebase_project_id):
                logger.info("✅ Firebase Admin SDK inicializado correctamente")
            else:
                logger.warning("⚠️ No se pudo inicializar Firebase Admin SDK. Las notificaciones push no estarán disponibles.")
        else:
            # Intentar inicialización por defecto (usa GOOGLE_APPLICATION_CREDENTIALS)
            if NotificacionesService.inicializar_firebase(None, firebase_project_id):
                logger.info("✅ Firebase Admin SDK inicializado con credenciales por defecto")
            else:
                if is_production:
                    logger.warning("⚠️ Firebase Admin SDK no configurado en producción. Configura FIREBASE_CREDENTIALS_PATH o FIREBASE_CREDENTIALS_JSON.")
                else:
                    logger.info("ℹ️ Firebase Admin SDK no configurado. Las notificaciones push no estarán disponibles hasta configurarlo.")
                    logger.info("   Coloca el archivo JSON de Firebase en la raíz del proyecto o configura FIREBASE_CREDENTIALS_PATH.")
    except Exception as e:
        logger.warning(f"⚠️ Error inicializando Firebase Admin SDK: {str(e)}. Las notificaciones push no estarán disponibles.")

    # Middleware de logging con timing
    @app.middleware("http")
    async def log_requests(request: Request, call_next: Any):
        start = time.perf_counter()
        logger.info(f"{request.client.host} -> {request.method} {request.url.path}")
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(f"{request.client.host} <- {request.method} {request.url.path} {duration_ms:.1f}ms")
        return response

    # Middleware para añadir headers de seguridad básicos a todas las respuestas
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    return app

# Instancia de la aplicación
app = create_application()

# Rutas base
@app.get("/")
async def root():
    """
    Ruta raíz que muestra información básica de la API
    """
    return {
        "message": "Service API",
        "version": settings.VERSION,
        "docs": "/docs"
    }

@app.get("/health")
async def health_check():
    """
    Endpoint para verificar el estado de la aplicación y la conexión a la BD
    """
    try:
        with get_db_connection() as conn:
            db_status = "connected" if conn else "disconnected"
    except Exception as e:
        logger.error(f"Error en health check: {str(e)}")
        db_status = "error"

    return {
        "status": "healthy",
        "version": settings.VERSION,
        "database": db_status
    }

# Compatibilidad con código existente
@app.get("/test")
async def test_db():
    try:
        with get_db_connection() as conn:
            if conn:
                return {"message": "Conexión exitosa"}
            else:
                return {"error": "Conexión fallida: objeto de conexión es None"}
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }

@app.get("/drivers")
async def check_drivers():
    """Endpoint para verificar drivers ODBC disponibles"""
    from app.db.connection import test_drivers
    drivers = test_drivers()
    return {
        "drivers_available": list(drivers),
        "odbc_17_found": 'ODBC Driver 17 for SQL Server' in drivers
    }

@app.get("/debug-env")
async def debug_env():
    """Endpoint para verificar variables de entorno (sin mostrar passwords)"""
    return {
        "db_server": settings.DB_SERVER,
        "db_user": settings.DB_USER,
        "db_database": settings.DB_DATABASE,
        "db_port": settings.DB_PORT,
        "db_password_set": bool(settings.DB_PASSWORD),
        "secret_key_set": bool(settings.SECRET_KEY),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.LOG_LEVEL.lower()
    )
