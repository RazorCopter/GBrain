from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.application.crypto import decrypt_json, encrypt_json
from gbrain.domain.models import SourceClientCredential


class SourceCredentialsRepository:
    """Repository per la gestione sicura del ciclo di vita delle credenziali client."""

    @staticmethod
    async def get_credentials(
        session: AsyncSession,
        owner_id: UUID,
        source_kind: str,
    ) -> SourceClientCredential | None:
        """Recupera il record delle credenziali client per il proprietario e la sorgente specificati."""
        return await session.scalar(
            select(SourceClientCredential).where(
                SourceClientCredential.owner_id == owner_id,
                SourceClientCredential.source_kind == source_kind,
            )
        )

    @staticmethod
    async def save_credentials(
        session: AsyncSession,
        owner_id: UUID,
        source_kind: str,
        *,
        auth_method: str = "oauth2",
        client_type: str = "web",
        client_id: str | None,
        encrypted_credentials: bytes,
        configured_redirect_uri: str | None = None,
        is_active: bool = True,
    ) -> SourceClientCredential:
        """Salva o aggiorna le credenziali cifrate per una determinata sorgente."""
        existing = await SourceCredentialsRepository.get_credentials(
            session, owner_id=owner_id, source_kind=source_kind
        )
        if existing is not None:
            existing.auth_method = auth_method
            existing.client_type = client_type
            existing.client_id = client_id
            existing.encrypted_credentials = encrypted_credentials
            existing.configured_redirect_uri = configured_redirect_uri
            existing.is_active = is_active
            await session.commit()
            await session.refresh(existing)
            return existing

        created = SourceClientCredential(
            owner_id=owner_id,
            source_kind=source_kind,
            auth_method=auth_method,
            client_type=client_type,
            client_id=client_id,
            encrypted_credentials=encrypted_credentials,
            configured_redirect_uri=configured_redirect_uri,
            is_active=is_active,
        )
        session.add(created)
        await session.commit()
        await session.refresh(created)
        return created

    @staticmethod
    async def get_decrypted_payload(
        session: AsyncSession,
        key: bytes,
        owner_id: UUID,
        source_kind: str,
    ) -> dict[str, Any] | None:
        """Recupera e decifra il dizionario JSON delle credenziali client se configurate e attive."""
        record = await SourceCredentialsRepository.get_credentials(
            session, owner_id=owner_id, source_kind=source_kind
        )
        if record is None or not record.is_active:
            return None
        return decrypt_json(record.encrypted_credentials, key)

    @staticmethod
    async def rotate_credentials(
        session: AsyncSession,
        owner_id: UUID,
        source_kind: str,
        *,
        old_key: bytes,
        new_key: bytes,
    ) -> SourceClientCredential | None:
        """Ricifra il payload delle credenziali memorizzate passando da una vecchia chiave ad una nuova."""
        record = await SourceCredentialsRepository.get_credentials(
            session, owner_id=owner_id, source_kind=source_kind
        )
        if record is None:
            return None

        decrypted = decrypt_json(record.encrypted_credentials, old_key)
        new_ciphertext = encrypt_json(decrypted, new_key)
        record.encrypted_credentials = new_ciphertext
        await session.commit()
        await session.refresh(record)
        return record

    @staticmethod
    async def delete_credentials(
        session: AsyncSession,
        owner_id: UUID,
        source_kind: str,
    ) -> bool:
        """Rimuove definitivamente le credenziali memorizzate per una sorgente."""
        existing = await SourceCredentialsRepository.get_credentials(
            session, owner_id=owner_id, source_kind=source_kind
        )
        if existing is None:
            return False
        await session.delete(existing)
        await session.commit()
        return True
