from datetime import date, datetime, time
from typing import Any, Dict, List, Optional
import logging

from app.db.queries import execute_query, execute_insert, execute_update, execute_transaction
from app.schemas.marcaciones import MarcacionRemotaCreate
from app.core.exceptions import NotFoundError, ValidationError
from app.services.base_service import BaseService

logger = logging.getLogger(__name__)

TIPOS_MARCACION_DIA = ("01", "02", "03", "04")
DIAS_SEMANA_ES = (
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
)


def _formato_hora_marca(valor: Any) -> Optional[str]:
    """Normaliza hora_marca de SQL Server a string 'HH:MM:SS'."""
    if valor is None:
        return None
    if isinstance(valor, time):
        return valor.replace(microsecond=0).strftime("%H:%M:%S")
    if isinstance(valor, datetime):
        return valor.time().replace(microsecond=0).strftime("%H:%M:%S")
    texto = str(valor).strip()
    if not texto:
        return None
    # p.ej. "08:30:00.0000000" o "08:30:00"
    return texto.split(".")[0][:8]


def _parse_fecha_marca(valor: Any) -> Optional[date]:
    """Convierte fecha_marca (date/datetime/str) a date."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()[:10]
    if not texto:
        return None
    return date.fromisoformat(texto)


def _dia_semana_es(fecha: date) -> str:
    return DIAS_SEMANA_ES[fecha.weekday()]


class MarcacionesService(BaseService):
    """Servicio de negocio para marcaciones remotas seguras."""

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_estado_hoy(codigo_trabajador: str) -> Dict[str, Optional[str]]:
        """
        Consulta marcaciones_remotas del día actual y mapea tipos 01-04 a su hora.
        """
        estado: Dict[str, Optional[str]] = {tipo: None for tipo in TIPOS_MARCACION_DIA}

        query = """
            SELECT id_tipo_marcacion, hora_marca
            FROM marcaciones_remotas
            WHERE codigo_trabajador = ?
              AND fecha_marca = CONVERT(date, GETDATE())
        """
        filas = execute_query(query, (codigo_trabajador,))

        for fila in filas:
            tipo = str(fila.get("id_tipo_marcacion") or "").strip()
            if tipo in estado and estado[tipo] is None:
                estado[tipo] = _formato_hora_marca(fila.get("hora_marca"))

        return estado

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_historial(codigo_trabajador: str, dias: int = 15) -> List[Dict[str, Any]]:
        """
        Retorna las marcaciones del trabajador en los últimos `dias` días,
        agrupadas por fecha con el mapa de tipos 01-04.
        Orden: fecha descendente.
        """
        if dias < 1:
            raise ValidationError(
                detail="El parámetro 'dias' debe ser mayor o igual a 1.",
                internal_code="DIAS_INVALIDO",
            )

        query = """
            SELECT fecha_marca, id_tipo_marcacion, hora_marca
            FROM marcaciones_remotas
            WHERE codigo_trabajador = ?
              AND fecha_marca >= DATEADD(day, -?, CONVERT(date, GETDATE()))
            ORDER BY fecha_marca DESC, id_tipo_marcacion ASC
        """
        filas = execute_query(query, (codigo_trabajador, dias))

        agrupado: Dict[str, Dict[str, Any]] = {}

        for fila in filas:
            fecha_obj = _parse_fecha_marca(fila.get("fecha_marca"))
            if fecha_obj is None:
                continue

            fecha_key = fecha_obj.isoformat()
            if fecha_key not in agrupado:
                agrupado[fecha_key] = {
                    "fecha": fecha_key,
                    "dia_semana": _dia_semana_es(fecha_obj),
                    "marcas": {tipo: None for tipo in TIPOS_MARCACION_DIA},
                }

            tipo = str(fila.get("id_tipo_marcacion") or "").strip()
            if tipo in TIPOS_MARCACION_DIA and agrupado[fecha_key]["marcas"][tipo] is None:
                agrupado[fecha_key]["marcas"][tipo] = _formato_hora_marca(fila.get("hora_marca"))

        return sorted(agrupado.values(), key=lambda item: item["fecha"], reverse=True)

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
