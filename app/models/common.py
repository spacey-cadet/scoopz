from pydantic import BaseModel


class HealthCheck(BaseModel):
    status: str
    app: str
    version: str


class ErrorResponse(BaseModel):
    detail: str

