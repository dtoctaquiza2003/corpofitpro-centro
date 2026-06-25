import json
import os
from typing import Any

import firebase_admin
from firebase_admin import credentials, messaging


def _firebase_app_inicializada() -> bool:
    return len(firebase_admin._apps) > 0


def inicializar_firebase() -> bool:
    if _firebase_app_inicializada():
        return True

    credentials_path = os.getenv("FIREBASE_CREDENTIALS_PATH")

    if not credentials_path:
        print("⚠️ FIREBASE_CREDENTIALS_PATH no está configurado.")
        return False

    if not os.path.exists(credentials_path):
        print(f"⚠️ No existe el archivo Firebase credentials: {credentials_path}")
        return False

    try:
        cred = credentials.Certificate(credentials_path)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin inicializado correctamente.")
        return True
    except Exception as e:
        print(f"❌ Error inicializando Firebase Admin: {e}")
        return False


def _data_to_string_map(data: dict[str, Any] | None) -> dict[str, str]:
    if not data:
        return {}

    resultado: dict[str, str] = {}

    for key, value in data.items():
        if value is None:
            continue

        if isinstance(value, (dict, list)):
            resultado[key] = json.dumps(value, ensure_ascii=False)
        else:
            resultado[key] = str(value)

    return resultado


def enviar_push_tokens(
    tokens: list[str],
    titulo: str,
    mensaje: str,
    data: dict[str, Any] | None = None,
) -> None:
    tokens_limpios = [
        token.strip()
        for token in tokens
        if token and token.strip()
    ]

    if not tokens_limpios:
        print("ℹ️ No hay tokens FCM activos para enviar push.")
        return

    if not inicializar_firebase():
        return

    try:
        message = messaging.MulticastMessage(
            tokens=tokens_limpios,
            notification=messaging.Notification(
                title=titulo,
                body=mensaje,
            ),
            data=_data_to_string_map(data),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="default_channel",
                ),
            ),
        )

        response = messaging.send_each_for_multicast(message)

        print(
            f"✅ Push FCM enviado. "
            f"Correctos: {response.success_count}, "
            f"Fallidos: {response.failure_count}"
        )

        if response.failure_count > 0:
            for idx, resp in enumerate(response.responses):
                if not resp.success:
                    print(
                        f"⚠️ Error enviando a token {idx}: "
                        f"{resp.exception}"
                    )

    except Exception as e:
        print(f"❌ Error enviando push FCM: {e}")