from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, description: str, error_code: int | None = None):
        self.method = method
        self.description = description
        self.error_code = error_code
        super().__init__(f"{method}: {description}")


@dataclass
class BotClient:
    token: str
    api_base: str = "https://api.telegram.org"

    @property
    def base_url(self) -> str:
        return f"{self.api_base}/bot{self.token}"

    def request(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> Any:
        url = f"{self.base_url}/{method}"
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(body)
                description = result.get("description", body)
            except json.JSONDecodeError:
                description = body
            raise TelegramApiError(method, description, exc.code) from exc
        except urllib.error.URLError as exc:
            raise TelegramApiError(method, str(exc.reason)) from exc

        if not result.get("ok"):
            raise TelegramApiError(method, result.get("description", "Unknown Telegram API error"))
        return result.get("result")

    def request_multipart(
        self,
        method: str,
        *,
        fields: dict[str, Any],
        files: dict[str, Path],
        timeout: int = 120,
    ) -> Any:
        url = f"{self.base_url}/{method}"
        boundary = f"----codex-{uuid4().hex}"
        body = bytearray()

        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")

        for key, path in files.items():
            filename = path.name
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                (
                    f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                    f"Content-Type: {mime}\r\n\r\n"
                ).encode("utf-8")
            )
            body.extend(path.read_bytes())
            body.extend(b"\r\n")

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            url,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(body_text)
                description = result.get("description", body_text)
            except json.JSONDecodeError:
                description = body_text
            raise TelegramApiError(method, description, exc.code) from exc
        except urllib.error.URLError as exc:
            raise TelegramApiError(method, str(exc.reason)) from exc

        if not result.get("ok"):
            raise TelegramApiError(method, result.get("description", "Unknown Telegram API error"))
        return result.get("result")

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": [
                "message",
                "callback_query",
                "pre_checkout_query",
            ],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload, timeout=timeout + 10)

    def get_me(self) -> dict[str, Any]:
        return self.request("getMe")

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.request("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, destination: Path, timeout: int = 120) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.api_base}/file/bot{self.token}/{file_path}"
        tmp_destination = destination.with_suffix(destination.suffix + ".tmp")
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                tmp_destination.write_bytes(response.read())
        except urllib.error.URLError as exc:
            raise TelegramApiError("downloadFile", str(exc.reason)) from exc
        os.replace(tmp_destination, destination)
        return destination

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

    def edit_message_caption(
        self,
        *,
        chat_id: int,
        message_id: int,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageCaption", payload)

    def edit_message_media(
        self,
        *,
        chat_id: int,
        message_id: int,
        media: dict[str, Any],
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": media,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageMedia", payload)

    def edit_message_media_upload(
        self,
        *,
        chat_id: int,
        message_id: int,
        media_type: str,
        media_path: Path,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | bool:
        media: dict[str, Any] = {
            "type": media_type,
            "media": "attach://media",
            "caption": caption,
            "parse_mode": parse_mode,
        }
        fields: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": media,
        }
        if reply_markup:
            fields["reply_markup"] = reply_markup
        return self.request_multipart(
            "editMessageMedia",
            fields=fields,
            files={"media": media_path},
        )

    def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendPhoto", payload)

    def upload_photo(
        self,
        chat_id: int,
        photo_path: Path,
        *,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            fields["reply_markup"] = reply_markup
        return self.request_multipart("sendPhoto", fields=fields, files={"photo": photo_path})

    def send_video(
        self,
        chat_id: int,
        video: str,
        *,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "video": video,
            "caption": caption,
            "parse_mode": parse_mode,
            "supports_streaming": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendVideo", payload)

    def upload_video(
        self,
        chat_id: int,
        video_path: Path,
        *,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": parse_mode,
            "supports_streaming": True,
        }
        if reply_markup:
            fields["reply_markup"] = reply_markup
        return self.request_multipart("sendVideo", fields=fields, files={"video": video_path})

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        return bool(self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        return bool(self.request("answerCallbackQuery", payload))

    def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        *,
        ok: bool,
        error_message: str = "",
    ) -> bool:
        payload: dict[str, Any] = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
        if error_message:
            payload["error_message"] = error_message
        return bool(self.request("answerPreCheckoutQuery", payload))

    def send_invoice(
        self,
        chat_id: int,
        *,
        title: str,
        description: str,
        payload: str,
        prices: list[dict[str, Any]],
        currency: str = "XTR",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invoice_payload: dict[str, Any] = {
            "chat_id": chat_id,
            "title": title[:32],
            "description": description[:255],
            "payload": payload,
            "currency": currency,
            "prices": prices,
            "provider_token": "",
            "start_parameter": "order",
        }
        if reply_markup:
            invoice_payload["reply_markup"] = reply_markup
        return self.request("sendInvoice", invoice_payload)


def run_polling(
    *,
    name: str,
    client: BotClient,
    timeout: int,
    handler,
    stop_on_error: bool = False,
    stop_event=None,
) -> None:
    offset: int | None = None
    started = False

    while stop_event is None or not stop_event.is_set():
        try:
            if not started:
                me = client.get_me()
                print(f"[{name}] @{me.get('username')} started")
                started = True
            updates = client.get_updates(offset=offset, timeout=timeout)
            for update in updates:
                try:
                    handler(update)
                    offset = int(update["update_id"]) + 1
                except Exception as exc:  # noqa: BLE001 - bot should keep polling
                    print(f"[{name}] update error: {exc}")
                    if update.get('message', {}).get('successful_payment'):
                        break
                    offset = int(update['update_id']) + 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - network polling should recover
            print(f"[{name}] polling error: {exc}")
            if isinstance(exc, TelegramApiError) and exc.error_code in {401, 404, 409}:
                if stop_event is not None:
                    stop_event.set()
                print(f'[{name}] fatal configuration/conflict error; workers stopping')
                return
            if stop_on_error:
                raise
            if stop_event is not None:
                stop_event.wait(3)
            else:
                time.sleep(3)
