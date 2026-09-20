#!/usr/bin/env python3
"""Пример плагина: токен от OAuth-провайдера, два вида на выбор.

Форма, из-за которой токен и приходится добывать скриптом: сперва учётные
данные меняются на токен пользователя, а потом — если серверу нужен токен
самого агента — тот обменивается на токен для нужной audience (RFC 8693).
Ручная вставка такого токена живёт до первого истечения; скрипт переживает.

    Token script:      examples/oauth_token_plugin.py
    Script arguments:  --env dev --token agent --person-id 1234567

Все три параметра обязательны и значений по умолчанию не имеют: молча
подставленная среда или вид токена — это токен не от того стенда, и узнаётся
об этом уже по отказу сервера.

Секретов в файле нет: пароль и секрет клиента читаются из окружения, которое
инспектор передаёт скрипту целиком, а адреса стендов можно переопределить
там же, не трогая код.

Только стандартная библиотека: файл без бита исполнения инспектор запускает
своим интерпретатором, а `requests` в его окружении может не оказаться. Если
нужен свой venv — `chmod +x` и шебанг на его python.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ENVIRONMENTS = {
    "dev": os.environ.get("TOKEN_URL_DEV", "https://auth.dev.example.com/oauth2/token"),
    "prod": os.environ.get("TOKEN_URL_PROD", "https://auth.example.com/oauth2/token"),
}

CLIENT_ID = "inspector"
EXCHANGE_CLIENT_ID = "inspector-agent"
AUDIENCE = "mcp-server"
USERNAME = "demo.user"

PASSWORD = os.environ.get("TOKEN_PASSWORD", "")
EXCHANGE_SECRET = os.environ.get("TOKEN_EXCHANGE_SECRET", "")

PERSON_HEADER = "X-Person-Id"
"""Чем провайдер узнаёт, от чьего имени просят токен."""

TIMEOUT = 20.0
"""Инспектор убивает скрипт через минуту, так что оба запроса должны успеть
раньше — иначе вместо ответа провайдера будет «did not finish»."""


def post_form(url: str, data: dict[str, str], headers: dict[str, str], what: str, insecure: bool) -> str:
    """POST формы, из ответа — `access_token`.

    Тело ошибки не проглатывается: провайдер объясняет отказ именно в нём
    (`invalid_grant`, `unauthorized_client`), а иначе в журнале инспектора
    осталось бы одно «400 Bad Request».
    """
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=TIMEOUT) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip().replace("\n", " ")
        raise SystemExit(f"{what}: {error.code} {error.reason} от {url} — {detail[:500]}") from None
    except urllib.error.URLError as error:
        raise SystemExit(f"{what}: не достучаться до {url} — {error.reason}") from None
    except json.JSONDecodeError:
        raise SystemExit(f"{what}: {url} ответил не JSON") from None

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise SystemExit(f"{what}: в ответе нет access_token (ключи: {', '.join(payload) or 'нет'})")
    return token


def user_token(url: str, person_id: str, insecure: bool) -> str:
    print(f"user-token: {url} от имени {person_id}", file=sys.stderr)
    return post_form(
        url,
        {"grant_type": "password", "client_id": CLIENT_ID, "username": USERNAME, "password": PASSWORD},
        {PERSON_HEADER: person_id},
        "user-token",
        insecure,
    )


def agent_token(url: str, subject_token: str, insecure: bool) -> str:
    """Обмен токена пользователя на токен агента, поэтому первый нужен всегда."""
    print(f"agent-token: обмен на audience {AUDIENCE}", file=sys.stderr)
    return post_form(
        url,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "audience": AUDIENCE,
            "client_id": EXCHANGE_CLIENT_ID,
            "client_secret": EXCHANGE_SECRET,
        },
        {},
        "agent-token",
        insecure,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, type=str.lower, choices=sorted(ENVIRONMENTS),
                        help="стенд, на котором берётся токен")
    parser.add_argument("--token", required=True, type=str.lower, choices=("user", "agent"),
                        help="чей токен напечатать")
    parser.add_argument("--person-id", required=True, metavar="ID",
                        help=f"значение заголовка {PERSON_HEADER}")
    parser.add_argument("--insecure", action="store_true",
                        help="не проверять сертификат: внутренний стенд со своим CA")
    args = parser.parse_args()

    if not PASSWORD or (args.token == "agent" and not EXCHANGE_SECRET):
        raise SystemExit(
            "нет учётных данных: задайте TOKEN_PASSWORD "
            "(и TOKEN_EXCHANGE_SECRET для agent) в окружении инспектора"
        )

    url = ENVIRONMENTS[args.env]
    token = user_token(url, args.person_id, args.insecure)
    if args.token == "agent":
        token = agent_token(url, token, args.insecure)

    # Единственная строка stdout — сам токен: инспектор кладёт её в поле Token.
    print(token)


if __name__ == "__main__":
    main()
