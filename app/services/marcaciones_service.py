from datetime import datetime
from typing import Optional
import logging

from app.db.queries import execute_query, execute_insert, execute_update, execute_transaction
from app.schemas.marcaciones import MarcacionRemotaCreate
from app.core.exceptions import NotFoundError
from app.services.base_service import BaseService

logger = logging.getLogger(__name__)


class MarcacionesService(BaseService):
    """Servicio de negocio para marcaciones remotas seguras."""

    @staticmethod
    @BaseService.handle_service_errors
    async def verificar_dispositivo(codigo_trabajador: str, token_dispositivo: str) -> bool:
        query = """
            SELECT 1 AS existe
            FROM dispositivos_usuarios
            WHERE codigo_trabajador = ?
              AND token_dispositivo = ?
              AND estado = 'A'
        """
        resultado = execute_query(query, (codigo_trabajador, token_dispositivo))
        return len(resultado) > 0

    @staticmethod
    @BaseService.handle_service_errors
    async def registrar_dispositivo(
        codigo_trabajador: str,
        token_dispositivo: str,
        modelo: Optional[str],
    ) -> None:
        update_query = """
            UPDATE dispositivos_usuarios
            SET estado = 'I'
            WHERE codigo_trabajador = ?
        """
        execute_update(update_query, (codigo_trabajador,))

        insert_query = """
            INSERT INTO dispositivos_usuarios (
                codigo_trabajador, token_dispositivo, modelo_dispositivo, estado
            )
            VALUES (?, ?, ?, 'A')
        """
        execute_insert(insert_query, (codigo_trabajador, token_dispositivo, modelo))

    @staticmethod
    @BaseService.handle_service_errors
    async def registrar_marcacion(
        codigo_trabajador: str,
        datos: MarcacionRemotaCreate,
        ip_cliente: str,
        cempre_defecto: str = "01",
    ) -> None:
        ahora = datetime.now()
        fecha_marca = ahora.date()
        hora_marca = ahora.time().replace(microsecond=0)

        dispositivo_query = """
            SELECT id_dispositivo
            FROM dispositivos_usuarios
            WHERE codigo_trabajador = ?
              AND token_dispositivo = ?
              AND estado = 'A'
        """
        dispositivo = execute_query(
            dispositivo_query,
            (codigo_trabajador, datos.token_dispositivo),
        )

        if not dispositivo:
            raise NotFoundError(
                detail="Dispositivo no registrado o inactivo para este trabajador.",
                internal_code="DISPOSITIVO_NO_REGISTRADO",
            )

        id_dispositivo = dispositivo[0]["id_dispositivo"]

        insert_legacy = """
            INSERT INTO marcaciones00 (cempre, ctraba, fmarca, hmarca, ctecla)
            VALUES (?, ?, ?, ?, ?)
        """
        insert_moderno = """
            INSERT INTO marcaciones_remotas (
                codigo_trabajador, id_tipo_marcacion, fecha_marca, hora_marca,
                id_dispositivo, latitud, longitud, direccion_ip
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        legacy_params = (
            cempre_defecto,
            codigo_trabajador,
            fecha_marca,
            hora_marca,
            datos.id_tipo_marcacion,
        )
        moderno_params = (
            codigo_trabajador,
            datos.id_tipo_marcacion,
            fecha_marca,
            hora_marca,
            id_dispositivo,
            datos.latitud,
            datos.longitud,
            ip_cliente,
        )

        def _dual_write(cursor) -> None:
            cursor.execute(insert_legacy, legacy_params)
            cursor.execute(insert_moderno, moderno_params)

        execute_transaction(_dual_write)

        logger.info(
            "Marcación remota registrada para trabajador %s (tipo=%s, dispositivo=%s)",
            codigo_trabajador,
            datos.id_tipo_marcacion,
            id_dispositivo,
        )
