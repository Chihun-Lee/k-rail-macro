"""Per-service credential storage in macOS Keychain.

Two namespaces — `config.srt` and `config.ktx` — each backed by an
isolated Keychain entry. Service names match the standalone
`srt-macro` and `ktx-macro` repos so an existing user keeps their
saved credentials when migrating to the unified app.
"""
from __future__ import annotations

import json
from typing import Optional

import keyring
from pydantic import BaseModel, Field


# ─── SRT ────────────────────────────────────────────────────────────────
class SRTCredentials(BaseModel):
    srt_id: str = Field(min_length=1)
    srt_password: str = Field(min_length=1)
    card_number: str = Field(min_length=12, max_length=19)
    card_password: str = Field(min_length=2, max_length=2)
    card_validation: str = Field(min_length=6, max_length=10)
    card_expire: str = Field(min_length=4, max_length=4)
    card_type: str = Field(default="J", pattern="^[JS]$")
    card_installment: int = Field(default=0, ge=0, le=24)


# ─── KTX ────────────────────────────────────────────────────────────────
class KTXCredentials(BaseModel):
    ktx_id: str = Field(min_length=1)
    ktx_password: str = Field(min_length=1)
    card_number: str = Field(default="", max_length=19)
    card_password: str = Field(default="", max_length=2)
    card_validation: str = Field(default="", max_length=10)
    card_expire: str = Field(default="", max_length=4)
    card_installment: int = Field(default=0, ge=0, le=24)


class _Namespace:
    def __init__(self, service: str, model: type[BaseModel]):
        self.service = service
        self.model = model
        self.user = "config"

    def _read_blob(self) -> Optional[str]:
        return keyring.get_password(self.service, self.user)

    def exists(self) -> bool:
        return self._read_blob() is not None

    def load(self):
        blob = self._read_blob()
        if not blob:
            return None
        try:
            return self.model.model_validate_json(blob)
        except Exception:
            return None

    def save(self, creds) -> None:
        payload = creds.model_dump()
        if "card_number" in payload:
            payload["card_number"] = payload["card_number"].replace("-", "").replace(" ", "")
        keyring.set_password(self.service, self.user, json.dumps(payload))

    def clear(self) -> None:
        try:
            keyring.delete_password(self.service, self.user)
        except keyring.errors.PasswordDeleteError:
            pass


class _SRT(_Namespace):
    def __init__(self):
        super().__init__("srt-macro", SRTCredentials)

    def public_status(self) -> dict:
        c = self.load()
        if not c:
            return {"configured": False}
        masked = "*" * (len(c.card_number) - 4) + c.card_number[-4:]
        return {
            "configured": True,
            "id": c.srt_id,
            "card_last4": c.card_number[-4:],
            "card_masked": masked,
            "card_type": c.card_type,
            "card_installment": c.card_installment,
            "storage": "macOS Keychain",
        }


class _KTX(_Namespace):
    def __init__(self):
        super().__init__("ktx-macro", KTXCredentials)

    def public_status(self) -> dict:
        c = self.load()
        if not c:
            return {"configured": False}
        has_card = bool(c.card_number)
        out = {
            "configured": True,
            "id": c.ktx_id,
            "has_card": has_card,
            "storage": "macOS Keychain",
        }
        if has_card:
            out["card_last4"] = c.card_number[-4:]
            out["card_masked"] = "*" * (len(c.card_number) - 4) + c.card_number[-4:]
            out["card_installment"] = c.card_installment
        return out


srt = _SRT()
ktx = _KTX()
