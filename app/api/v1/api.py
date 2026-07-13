from fastapi import APIRouter
from app.api.v1.endpoints import (
    usuarios,
    auth,
    menus,
    roles,
    permisos,
    areas,
    autorizacion,
    vacaciones_permisos_mobile,
    vacaciones_permisos_admin,
    notificaciones,
    avisos_ap,
    clientes,
    aplicaciones,
    cliente_conexiones,
    conexion,
    marcaciones,
)

api_router = APIRouter()

api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Autenticación"]
)

api_router.include_router(
    usuarios.router,
    prefix="/usuarios",
    tags=["Usuarios"]
)

api_router.include_router(
    menus.router,
    prefix="/menus",
    tags=["Menus"]
)

api_router.include_router(
    roles.router, 
    prefix="/roles", 
    tags=["Roles"]
    )

api_router.include_router(
    permisos.router, 
    prefix="/permisos", 
    tags=["Permisos (Rol-Menú)"]
    )

api_router.include_router(
    areas.router, 
    prefix="/areas", 
    tags=["Areas"]
    )

api_router.include_router(
    autorizacion.router,
    prefix="/autorizacion",
    tags=["Autorización de Procesos"]
)

api_router.include_router(
    vacaciones_permisos_mobile.router,
    prefix="/vacaciones",
    tags=["Vacaciones y Permisos (Móvil)"]
)

api_router.include_router(
    vacaciones_permisos_admin.router,
    prefix="/vacaciones/admin",
    tags=["Vacaciones y Permisos (SuperAdmin)"]
)

api_router.include_router(
    notificaciones.router,
    prefix="/notificaciones",
    tags=["Notificaciones Push"]
)

api_router.include_router(
    avisos_ap.router,
    prefix="/avisos",
    tags=["Avisos (AP)"]
)

api_router.include_router(
    clientes.router,
    prefix="/clientes",
    tags=["Clientes"]
)

api_router.include_router(
    aplicaciones.router,
    prefix="/aplicaciones",
    tags=["Aplicaciones"]
)

api_router.include_router(
    cliente_conexiones.router,
    prefix="/clientes",
    tags=["Conexiones (Cliente)"]
)

api_router.include_router(
    conexion.router,
    prefix="/conexion",
    tags=["Conexión (Apps)"]
)

api_router.include_router(
    marcaciones.router,
    prefix="/marcaciones",
    tags=["Marcaciones Remotas"]
)