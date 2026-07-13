from pydantic import BaseModel
from typing import Optional


class VerificarDispositivoResponse(BaseModel):
    registrado: bool


class RegistrarDispositivoRequest(BaseModel):
    token_dispositivo: str
    modelo_dispositivo: Optional[str] = None


class MarcacionRemotaCreate(BaseModel):
    id_tipo_marcacion: str  # Ej: '01', '02'
    token_dispositivo: str
    latitud: float
    longitud: float
