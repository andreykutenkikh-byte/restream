"""Strict API request and response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class MediaMTXAuthRequest(StrictModel):
    user: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=2048)
    token: str = Field(default="", max_length=2048)
    ip: str = Field(default="", max_length=128)
    action: str = Field(max_length=32)
    path: str = Field(default="", max_length=1024)
    protocol: str = Field(default="", max_length=32)
    # MediaMTX serializes a missing protocol connection ID as JSON null.
    id: str | None = Field(default=None, max_length=256)
    query: str = Field(default="", max_length=2048)
    userAgent: str = Field(default="", max_length=1024)  # noqa: N815


class DestinationCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    server_url: str = Field(min_length=8, max_length=1024)
    stream_key: str = Field(min_length=1, max_length=1024)
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("Название содержит недопустимые символы")
        return value


class DestinationUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    server_url: str | None = Field(default=None, min_length=8, max_length=1024)
    stream_key: str | None = Field(default=None, min_length=1, max_length=1024)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("Название содержит недопустимые символы")
        return value
