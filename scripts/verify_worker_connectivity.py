"""Verifica connettività DNS e HTTPS innocua verso gli endpoint Google dal container worker.

Nessun token, credenziale, segreto o email viene inviato.
"""

from __future__ import annotations

import socket
import ssl
import sys
import urllib.request

HOSTS = [
    "oauth2.googleapis.com",
    "gmail.googleapis.com",
]


def test_connectivity() -> bool:
    success = True
    print("--- 1. Verifica Risoluzione DNS ---")
    for host in HOSTS:
        try:
            ip = socket.gethostbyname(host)
            print(f"[OK] DNS risolto: {host} -> {ip}")
        except Exception as exc:
            print(f"[FAIL] DNS fallito per {host}: {exc}")
            success = False

    print("\n--- 2. Verifica Handshake TLS / HTTPS (porta 443, no credenziali) ---")
    for host in HOSTS:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as raw_sock:
                with ctx.wrap_socket(raw_sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
                    subject = cert.get("subject") if cert else None
                    print(f"[OK] TLS handshake completato con {host} (subject: {subject})")
        except Exception as exc:
            print(f"[FAIL] TLS fallito per {host}: {exc}")
            success = False

    print("\n--- 3. Richiesta HTTP innocua (GET /generate_204 o OPTIONS, no auth) ---")
    for host in HOSTS:
        try:
            req = urllib.request.Request(
                f"https://{host}/",
                headers={"User-Agent": "GBrain-Connectivity-Probe/1.0"},
                method="HEAD",
            )
            # URL costruito esclusivamente dalla allowlist HOSTS e vincolato a HTTPS.
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                print(f"[OK] HTTPS HEAD su https://{host}/ -> Status {resp.status}")
        except urllib.error.HTTPError as exc:
            # Anche un HTTP 404/405/400 dimostra connettività TLS e livello applicativo funzionante
            print(
                f"[OK] HTTPS risposta remota da https://{host}/ -> Status {exc.code} (atteso per endpoint protetto senza credenziali)"
            )
        except Exception as exc:
            print(f"[FAIL] HTTPS richiesta fallita per https://{host}/: {exc}")
            success = False

    return success


if __name__ == "__main__":
    ok = test_connectivity()
    if not ok:
        sys.exit(1)
    print("\n[SUCCESS] Tutte le verifiche DNS e HTTPS verso Google sono riuscite dal worker!")
