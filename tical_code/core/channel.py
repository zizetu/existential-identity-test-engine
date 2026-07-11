# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Original repository: https://github.com/zizetu/eite-agent
#

"""channel layer - message send/receive abstraction.

Channel hierarchy:
  Channel (abstract base)
   ├── TelegramChannel   – polls Telegram Bot API (TG_BOT_TOKEN env)
   └── TicalChatChannel  – polls tical-chat HTTP API (TICAL_CHAT_URL + TICAL_CHAT_KEY env)

To add a custom channel: subclass Channel, implement poll() + send(),
then wire it into unified_worker.py's channel init block.
All credentials are read from environment variables at runtime.
"""

import os
import json
import logging
import urllib.request, urllib.error
import ssl
import tempfile
import subprocess
import zipfile
import io
import re
from pathlib import Path
from typing import Optional

from tical_code.core.security_baseline import _check_ssrf

_UA = "eite-agent/0.1.5 (Cloudflare bypass)"
logger = logging.getLogger("EITElite.channel")


def _ssrf_guard(url_or_req, private_ok=False):
    """SSRF check before urlopen. Raises ValueError if blocked.

    localhost / 127.0.0.1 / ::1 are always exempt (internal services).
    When private_ok=True, private/RFC1918 addresses are allowed (opt-in).
    """
    url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
    # LIVE 2026-07-09p: localhost exemption for internal services
    try:
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(url)
        _host = (_parsed.hostname or "").lower()
        if _host in ("localhost", "127.0.0.1", "::1"):
            return  # localhost always allowed
    except Exception:
        pass
    if private_ok:
        return
    _check_ssrf(url)  # raises ValueError on failure, returns None on success

class Message:
    """Unified message format."""
    def __init__(self, sender: str, content: str, source: str = "telegram",
                 chat_id: Optional[str] = None, raw: Optional[dict] = None,
                 media_data: Optional[list] = None):
        self.sender = sender
        self.content = content
        self.source = source
        self.chat_id = chat_id
        self.raw = raw or {}
        self.media_data = media_data or []  # [{"type":"image","mime":"image/png","data":"base64..."}, ...]


class Response:
    """Unified response format."""
    def __init__(self, content: str, target: str, source: str = "telegram",
                 chat_id: Optional[str] = None, raw: Optional[dict] = None):
        self.content = content
        self.target = target
        self.source = source
        self.chat_id = chat_id
        self.raw = raw or {}


class Channel:
    """Message channel abstract base class."""
    def poll(self) -> list[Message]:
        raise NotImplementedError


class TelegramChannel(Channel):
    def __init__(self, token: str):
        self._api = f"https://api.telegram.org/bot{token}"
        self._last_update = 0
        self._telegram_file_api = f"https://api.telegram.org/file/bot{token}"
        token_preview = token[:6] + "..." + token[-4:] if len(token) > 12 else "(invalid)"
        logger.info("Telegram channel initialized (token: %s)", token_preview)
        # Lazy-load STT model on first voice message
        self._stt_model = None

    def _transcribe_ogg(self, ogg_path: str) -> str:
        """Transcribe OGG audio file to text using faster-whisper."""
        try:
            import faster_whisper  # optional — install with [full] extras
        except ImportError:
            logger.warning("faster-whisper not installed — voice transcription unavailable. pip install eite-agent[full]")
            return ""
        try:
            if self._stt_model is None:
                self._stt_model = faster_whisper.WhisperModel("tiny", device="cpu", compute_type="int8")
                logger.info("STT model loaded (tiny)")
            segments, _ = self._stt_model.transcribe(ogg_path, beam_size=5, language="zh")
            text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
            logger.info(f"STT transcribe: {len(text)} chars")
            return text
        except Exception as e:
            logger.warning(f"STT error: {e}")
            return ""

    def _extract_docx_text(self, file_bytes: bytes) -> str:
        """Extract plain text from .docx file (ZIP-compressed XML)."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(file_bytes))
            xml_content = zf.read("word/document.xml")
            zf.close()
            text = re.sub(r"<[^>]+>", " ", xml_content.decode("utf-8", errors="replace"))
            text = re.sub(r"\s+", " ", text).strip()
            return text if text else ""
        except Exception as e:
            logger.warning(f"docx extract error: {e}")
            return ""

    def _extract_pdf_text(self, file_bytes: bytes) -> str:
        """Extract plain text from .pdf file using pdftotext (poppler-utils)."""
        if subprocess.call(["which", "pdftotext"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            logger.warning("pdftotext not found — PDF extraction unavailable. Install poppler-utils")
            return ""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ["pdftotext", "-layout", tmp_path, "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ""
        except Exception as e:
            logger.warning(f"pdf extract error: {e}")
            return ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _save_binary_doc(self, file_path: str, doc_data: bytes, media_list: list) -> None:
        """Save binary document to temp and add to media_list with type note."""
        import uuid
        fname = file_path.split("/")[-1]
        tmp_path = f"/tmp/uploads/{uuid.uuid4().hex}_{fname}"
        os.makedirs("/tmp/uploads", exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(doc_data)
        ext = os.path.splitext(fname)[1].lower() if "." in fname else ""
        note = "binary file"
        if ext in (".zip", ".tar", ".tar.gz", ".tgz", ".gz"):
            note = "archive file (use: unzip/unpigz/tar xf)"
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"):
            note = "image file"
        elif ext in (".pdf",):
            note = "PDF document"
        elif ext in (".doc", ".docx", ".xls", ".xlsx"):
            note = "Office document"
        elif ext in (".mp3", ".wav", ".ogg", ".flac"):
            note = "audio file"
        elif ext in (".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp"):
            note = "source code file"
        media_list.append({"type": "binary_saved", "filename": fname, "path": tmp_path, "note": note, "ext": ext})

    def _download_media(self, msg: dict) -> list:
        """Download photo/document from Telegram, return [{"type":"image","mime":"...","data":"base64..."}]"""
        import base64, urllib.request
        media_list = []
        try:
            # Photo: take largest size (last in array)
            photo = msg.get("photo")
            if photo:
                file_id = photo[-1]["file_id"]
                _ssrf_guard(f"{self._api}/getFile?file_id={file_id}")
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    _ssrf_guard(f"{self._telegram_file_api}/{file_path}")
                    img_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=15).read()
                    b64 = base64.b64encode(img_data).decode()
                    mime = "image/jpeg"
                    if file_path.endswith(".png"): mime = "image/png"
                    elif file_path.endswith(".gif"): mime = "image/gif"
                    elif file_path.endswith(".webp"): mime = "image/webp"
                    media_list.append({"type": "image", "mime": mime, "data": b64})
                    logger.info(f"tg media: downloaded photo ({len(img_data)} bytes)")

            # Voice: download and transcribe
            voice = msg.get("voice")
            if voice:
                file_id = voice["file_id"]
                _ssrf_guard(f"{self._api}/getFile?file_id={file_id}")
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    _ssrf_guard(f"{self._telegram_file_api}/{file_path}")
                    ogg_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=30).read()
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                        f.write(ogg_data)
                        tmp_path = f.name
                    try:
                        transcript = self._transcribe_ogg(tmp_path)
                        if transcript:
                            media_list.append({"type": "transcript", "text": transcript})
                            logger.info(f"tg media: transcribed voice ({len(transcript)} chars)")
                    finally:
                        os.unlink(tmp_path)

            # Audio (music file): same as voice
            audio = msg.get("audio")
            if audio and not voice:
                file_id = audio["file_id"]
                _ssrf_guard(f"{self._api}/getFile?file_id={file_id}")
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    _ssrf_guard(f"{self._telegram_file_api}/{file_path}")
                    audio_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=30).read()
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                        f.write(audio_data)
                        tmp_path = f.name
                    try:
                        transcript = self._transcribe_ogg(tmp_path)
                        if transcript:
                            media_list.append({"type": "transcript", "text": transcript})
                            logger.info(f"tg media: transcribed audio ({len(transcript)} chars)")
                    finally:
                        os.unlink(tmp_path)

            # Document: download and extract text
            doc = msg.get("document")
            if doc:
                file_id = doc["file_id"]
                _ssrf_guard(f"{self._api}/getFile?file_id={file_id}")
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    fname = file_path.lower()
                    _ssrf_guard(f"{self._telegram_file_api}/{file_path}")
                    doc_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=30).read()
                    if doc_data:
                        if fname.endswith((".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log", ".py", ".js", ".html", ".css", ".sh")):
                            text = doc_data.decode("utf-8", errors="replace")
                            media_list.append({"type": "document_text", "text": text[:10000], "filename": file_path.split("/")[-1]})
                            logger.info("tg media: read doc (%d chars, capped to 10k)", len(text))
                        elif fname.endswith((".docx", ".doc")):
                            text = self._extract_docx_text(doc_data)
                            if text:
                                media_list.append({"type": "document_text", "text": text[:10000], "filename": file_path.split("/")[-1]})
                                logger.info("tg media: extracted docx (%d chars, capped to 10k)", len(text))
                            else:
                                # Fallback: save as binary
                                self._save_binary_doc(file_path, doc_data, media_list)
                        elif fname.endswith(".pdf"):
                            text = self._extract_pdf_text(doc_data)
                            if text:
                                media_list.append({"type": "document_text", "text": text[:10000], "filename": file_path.split("/")[-1]})
                                logger.info("tg media: extracted pdf (%d chars, capped to 10k)", len(text))
                            else:
                                self._save_binary_doc(file_path, doc_data, media_list)
                        else:
                            self._save_binary_doc(file_path, doc_data, media_list)
        except Exception as e:
            logger.warning(f"tg_media_download error: {e}")
        return media_list

    def poll(self) -> list[Message]:
        import urllib.request
        try:
            url = f"{self._api}/getUpdates?offset={self._last_update + 1}&timeout=5"
            _ssrf_guard(url)
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            msgs = []
            for u in data.get("result", []):
                self._last_update = u["update_id"]
                msg = u.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not chat_id:
                    continue
                text = msg.get("text") or msg.get("caption") or ""
                text = text.strip()
                # Download media for supported types (photo only for now)
                media_data = self._download_media(msg) if (msg.get("photo") or msg.get("voice") or msg.get("audio") or msg.get("document")) else []
                # Build media annotation for text
                media_types = []
                has_actual_media = bool(media_data)
                if has_actual_media:
                    for md in media_data:
                        if md["type"] == "image": media_types.append("image")
                        elif md["type"] == "transcript": media_types.append("voice (transcribed)")
                        elif md["type"] == "document_text": media_types.append("File content (read)")
                        elif md["type"] == "document": media_types.append("File (binary)")
                        elif md["type"] == "binary_saved": media_types.append("File (Saved: " + md.get("filename","") + ")")
                        else: media_types.append("media")
                if msg.get("video"): media_types.append("video")
                if media_types:
                    if has_actual_media:
                        note = " (User sent " + ", ".join(media_types) + ", loaded and ready to check)"
                    else:
                        note = " (User sent " + ", ".join(media_types) + ", cannot check)"
                    if text:
                        text = text + " " + note
                    else:
                        text = note
                if text and chat_id:
                    msgs.append(Message(sender="user", content=text,
                                        source="telegram", chat_id=chat_id,
                                        raw=msg, media_data=media_data))
            return msgs
        except Exception as e:
            logger.warning(f"tg_poll error: {e}")
            return []

    def send_action(self, action: str = "typing", chat_id: str = "") -> bool:
        """Send chat action (typing, upload_photo, etc.) to show activity."""
        if not chat_id:
            return False
        try:
            data = json.dumps({"chat_id": chat_id, "action": action}).encode()
            req = urllib.request.Request(
                f"{self._api}/sendChatAction", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            _ssrf_guard(req)
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception as e:
            logger.warning(f"tg_action error: {e}")
            return False

    def send(self, response: Response) -> bool:
        # Sanitize outbound: never ship fence-spam / garbage dumps to Telegram
        try:
            from tical_code.core.response_formatter import sanitize_outbound_reply
            if response is not None and getattr(response, "content", None):
                response.content = sanitize_outbound_reply(response.content)
        except Exception:
            pass
        try:
            data = json.dumps({"chat_id": response.chat_id,
                               "text": response.content[:4000]}).encode()
            req = urllib.request.Request(
                f"{self._api}/sendMessage", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            _ssrf_guard(req)
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                if not body.get("ok"):
                    logger.warning(f"tg_send api_error: {body.get('description','?')} chat_id={response.chat_id}")
                    return False
                return True
        except Exception as e:
            logger.warning(f"tg_send error: {e} chat_id={response.chat_id}")
            return False


class TicalChatChannel(Channel):
    def __init__(self, base_url: str = "http://localhost:8080",
                 identity: str = os.environ.get("WORKER_NAME", "agent"), shared_key: str = os.environ.get("TICAL_CHAT_KEY", ""),
                 api_key: str = None):
        if api_key is not None:
            shared_key = api_key
        self._url = base_url.rstrip("/")
        self._identity = identity
        self._key = shared_key
        self._since = 0.0
        logger.info(f"tical-chat channel initialized: identity={identity} on {base_url}")

    def poll(self) -> list[Message]:
        try:
            url = f"{self._url}/v1/messages?since={self._since}&limit=5"
            req = urllib.request.Request(
                url, headers={"X-AI-Identity": self._identity,
                              "X-AI-Key": self._key,
                              "User-Agent": _UA})
            _ssrf_guard(req)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            msgs = []
            for m in data.get("messages", []):
                if m.get("timestamp", 0) > self._since:
                    self._since = m["timestamp"]
                # Only process messages from real users (not other worker AI replies)
                sender = m.get("sender", "unknown")
                if sender not in ("user", "web-user"):
                    continue
                # Only process messages targeted to this worker (or broadcast with empty target)
                target = m.get("target", "")
                if target and target != self._identity:
                    continue
                msgs.append(Message(
                    sender=sender,
                    content=m.get("content", ""),
                    source="tical-chat",
                    raw=m))
            return msgs
        except urllib.error.URLError as e:
            code = getattr(e, 'code', 0) or 0
            if code in (401, 403):
                now_m = int(time.time()) // 300
                if getattr(self, '_chat_poll_auth_ts', -1) != now_m:
                    self._chat_poll_auth_ts = now_m
                    logger.error(f"chat_poll auth error (HTTP {code}): {e}")
            else:
                logger.debug(f"chat_poll URLError: {e}")
            return []
        except (ConnectionError, ConnectionRefusedError, TimeoutError) as e:
            logger.error(f"chat_poll error: {e}")
            return []
        except Exception as e:
            logger.error(f"chat_poll error: {e}")
            return []

    def send(self, response: Response) -> bool:
        """Directly send to message queue, not go through LLM inference. AI-to-AI messages use POST /v1/messages."""
        return self._send(response)

    def reply(self, response: Response) -> bool:
        """Alias for send(). Used by EITE-benchmark tests. Logs errors on failure."""
        result = self._send(response)
        if not result:
            logger.error("chat_reply failed")
        return result

    def _send(self, response: Response) -> bool:
        try:
            payload = json.dumps({
                "sender": self._identity,
                "target": response.target,
                "content": response.content,
            }).encode()
            req = urllib.request.Request(
                f"{self._url}/v1/messages", data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-AI-Identity": self._identity,
                    "X-AI-Key": self._key,
                    "User-Agent": _UA,
                }, method="POST")
            _ssrf_guard(req)
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            logger.error(f"chat_send error: {e}")
            return False

    def reconnect(self, max_retries: int = 3, backoff: float = 2.0) -> bool:
        """Reconnect with exponential backoff. Retries on connection failure."""
        import time
        for attempt in range(max_retries):
            try:
                # Test connection by polling
                url = f"{self._url}/v1/messages?since=0&limit=1"
                req = urllib.request.Request(
                    url, headers={"X-AI-Identity": self._identity, "X-AI-Key": self._key,
                                  "User-Agent": _UA})
                _ssrf_guard(req)
                urllib.request.urlopen(req, timeout=5)
                logger.info("reconnect successful")
                return True
            except Exception as e:
                wait = backoff ** attempt
                logger.warning(f"retry {attempt+1}/{max_retries} in {wait}s: {e}")
                if attempt < max_retries - 1:
                    time.sleep(wait)
        logger.error("reconnect failed after all retries")
        return False