"""Local OAuth sign-in and credential management for ChatGPT plan usage."""

from __future__ import annotations

import services.system_tls

import base64
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import http.server
import json
import os
import queue
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any


AUTH_BASE = "https://auth.openai.com"
API_RESOURCE = "https://api.openai.com/v1"
INITIAL_CLIENT_ID = "dynamic_agent_client"
REQUIRED_SCOPES = {
    "chatgpt.tokens.use.direct",
    "offline_access",
    "resource.invoke",
}
KEYRING_SERVICE = "nyabot-chatgpt-plan"
CALLBACK_PATH = "/auth/callback"
BASE_DIR = Path(__file__).resolve().parent.parent
METADATA_FILE = BASE_DIR / "data" / "chatgpt_auth.json"
LOCK_FILE = BASE_DIR / "data" / "chatgpt_auth.lock"
WINDOWS_CREDENTIAL_FILE = BASE_DIR / "data" / "chatgpt_credentials.dpapi"

_refresh_lock = threading.Lock()


class ChatGPTAuthError(RuntimeError):
    """Raised when a local ChatGPT plan connection is unavailable."""


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise ChatGPTAuthError(
            "Credential storage is unavailable. Install project dependencies, "
            "including keyring, then sign in again."
        ) from exc
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        raise ChatGPTAuthError(f"Could not initialize the OS credential manager: {exc}") from exc
    if getattr(backend, "priority", 0) <= 0:
        raise ChatGPTAuthError(
            "No secure OS credential manager is available. Configure Windows Credential Manager, "
            "then retry."
        )
    return keyring


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_crypt(data: bytes, *, decrypt: bool) -> bytes:
    """Encrypt/decrypt credential bytes for the current Windows user."""
    if os.name != "nt":
        raise ChatGPTAuthError("Windows credential protection is only available on Windows.")

    source = ctypes.create_string_buffer(data, max(1, len(data)))
    source_blob = _DataBlob(
        len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte))
    )
    result_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if decrypt:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        operation.restype = ctypes.wintypes.BOOL
        succeeded = operation(
            ctypes.byref(source_blob), None, None, None, None, 0x1, ctypes.byref(result_blob)
        )
    else:
        operation = crypt32.CryptProtectData
        operation.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        operation.restype = ctypes.wintypes.BOOL
        succeeded = operation(
            ctypes.byref(source_blob),
            "Nyabot ChatGPT credentials",
            None,
            None,
            None,
            0x1,
            ctypes.byref(result_blob),
        )

    if not succeeded:
        error = ctypes.WinError(ctypes.get_last_error())
        raise ChatGPTAuthError(f"Windows could not protect the ChatGPT credentials: {error}")
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(result_blob.pbData)


def _write_windows_credentials(values: dict[str, str]) -> None:
    WINDOWS_CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected = _dpapi_crypt(payload, decrypt=False)
    temporary = WINDOWS_CREDENTIAL_FILE.with_suffix(".dpapi.tmp")
    with temporary.open("wb") as file:
        file.write(protected)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, WINDOWS_CREDENTIAL_FILE)


def _read_windows_credentials() -> dict[str, str]:
    if WINDOWS_CREDENTIAL_FILE.exists():
        try:
            protected = WINDOWS_CREDENTIAL_FILE.read_bytes()
            values = json.loads(_dpapi_crypt(protected, decrypt=True).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatGPTAuthError) as exc:
            raise ChatGPTAuthError(f"Cannot read protected ChatGPT credentials: {exc}") from exc
        if not isinstance(values, dict) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise ChatGPTAuthError("Protected ChatGPT credentials are invalid; run login again.")
        return values

    # Migrate credentials from older Nyabot versions that used Windows Credential Manager.
    try:
        keyring = _keyring()
    except ChatGPTAuthError:
        return {}
    legacy: dict[str, str] = {}
    for name in ("access_token", "refresh_token"):
        try:
            value = keyring.get_password(KEYRING_SERVICE, name)
        except Exception:
            continue
        if value:
            legacy[name] = value
    if legacy:
        _write_windows_credentials(legacy)
        for name in legacy:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
    return legacy


def _load_metadata() -> dict[str, Any]:
    try:
        with METADATA_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatGPTAuthError(f"Cannot read ChatGPT sign-in metadata: {exc}") from exc
    if not isinstance(data, dict):
        raise ChatGPTAuthError("ChatGPT sign-in metadata is invalid; run login again.")
    return data


def _save_metadata(data: dict[str, Any]) -> None:
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = METADATA_FILE.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, METADATA_FILE)


@contextlib.contextmanager
def _credential_file_lock():
    """Serialize token refreshes across separate Nyabot processes."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+b") as file:
        if os.name == "nt":
            import msvcrt

            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _set_credential(name: str, value: str) -> None:
    if os.name == "nt":
        credentials = _read_windows_credentials()
        credentials[name] = value
        _write_windows_credentials(credentials)
        return
    try:
        _keyring().set_password(KEYRING_SERVICE, name, value)
    except ChatGPTAuthError:
        raise
    except Exception as exc:
        raise ChatGPTAuthError(f"Could not save ChatGPT credentials to the OS credential manager: {exc}") from exc


def _get_credential(name: str) -> str | None:
    if os.name == "nt":
        return _read_windows_credentials().get(name)
    try:
        return _keyring().get_password(KEYRING_SERVICE, name)
    except ChatGPTAuthError:
        raise
    except Exception as exc:
        raise ChatGPTAuthError(f"Could not read ChatGPT credentials from the OS credential manager: {exc}") from exc


def _delete_credentials() -> None:
    if os.name == "nt":
        WINDOWS_CREDENTIAL_FILE.unlink(missing_ok=True)
        try:
            keyring = _keyring()
        except ChatGPTAuthError:
            return
        for name in ("access_token", "refresh_token"):
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        return
    keyring = _keyring()
    for name in ("access_token", "refresh_token"):
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            raise ChatGPTAuthError(f"Could not remove credentials from the OS credential manager: {exc}") from exc


def _form_request(url: str, values: dict[str, str], *, timeout: int = 20) -> dict[str, Any]:
    body = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            error = detail.get("error", detail) if isinstance(detail, dict) else {}
            message = error.get("error_description") or error.get("message") or error.get("error")
        except Exception:
            message = None
        suffix = f": {message}" if message else ""
        raise ChatGPTAuthError(f"ChatGPT authorization failed (HTTP {exc.code}){suffix}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ChatGPTAuthError(f"Could not reach ChatGPT authorization service: {exc}") from exc

    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatGPTAuthError("ChatGPT authorization service returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise ChatGPTAuthError("ChatGPT authorization service returned an invalid response.")
    return result


def _openid_configuration() -> dict[str, Any]:
    url = f"{AUTH_BASE}/.well-known/openid-configuration"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ChatGPTAuthError(f"Could not load OpenAI sign-in configuration: {exc}") from exc
    if not isinstance(result, dict) or not result.get("jwks_uri"):
        raise ChatGPTAuthError("OpenAI sign-in configuration is missing its JWKS endpoint.")
    return result


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _verify_id_token(token: str, *, client_id: str, nonce: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        import jwt
    except ImportError as exc:
        raise ChatGPTAuthError(
            "ID token verification is unavailable. Install project dependencies, "
            "including PyJWT[crypto], then sign in again."
        ) from exc

    try:
        jwks = jwt.PyJWKClient(config["jwks_uri"])
        signing_key = jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[signing_key.algorithm_name],
            audience=client_id,
            issuer=config.get("issuer", AUTH_BASE),
            leeway=60,
            options={"require": ["exp", "iss", "sub", "aud", "nonce"]},
        )
    except Exception as exc:
        raise ChatGPTAuthError(f"Could not validate the ChatGPT sign-in identity: {exc}") from exc
    if claims.get("nonce") != nonce:
        raise ChatGPTAuthError("ChatGPT sign-in nonce did not match; run login again.")
    return claims


def _callback_server(expected_state: str):
    callback: queue.Queue[dict[str, list[str]]] = queue.Queue(maxsize=1)

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if values.get("state", [None])[0] != expected_state:
                self.send_error(400, "OAuth state mismatch")
                return
            try:
                callback.put_nowait(values)
            except queue.Full:
                self.send_error(409, "Authorization callback already received")
                return

            page = (
                "<!doctype html><meta charset='utf-8'><title>Nyabot sign-in</title>"
                "<p>ChatGPT sign-in received. You can close this tab and return to Nyabot.</p>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    server.timeout = 1
    return server, callback


def login() -> None:
    metadata = _load_metadata()
    host_id = metadata.get("host_id") or f"urn:uuid:{uuid.uuid4()}"
    metadata["host_id"] = host_id
    _save_metadata(metadata)

    issued_client_id = metadata.get("client_id")
    client_id = issued_client_id or INITIAL_CLIENT_ID
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = _base64url(secrets.token_bytes(48))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    server, callback = _callback_server(state)
    redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": API_RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "ext_agent_host_id": host_id,
    }
    if issued_client_id:
        if metadata.get("email"):
            params["login_hint"] = metadata["email"]
    else:
        params["agent_name_hint"] = "Nyabot"

    authorization_url = f"{AUTH_BASE}/api/accounts/authorize?{urllib.parse.urlencode(params)}"
    print("Opening your browser for ChatGPT sign-in. Approve Nyabot's requested plan access there.")
    print("If it does not open, copy this URL into your browser:\n" + authorization_url)
    webbrowser.open(authorization_url)

    values = None
    deadline = time.monotonic() + 300
    try:
        while time.monotonic() < deadline:
            server.handle_request()
            try:
                values = callback.get_nowait()
                break
            except queue.Empty:
                continue
    finally:
        server.server_close()

    if not values:
        raise ChatGPTAuthError("Timed out waiting for the browser sign-in callback.")
    if values.get("error"):
        error = values["error"][0]
        description = values.get("error_description", [""])[0]
        raise ChatGPTAuthError(f"ChatGPT sign-in was not completed: {error} {description}".strip())

    code = values.get("code", [None])[0]
    returned_client_id = values.get("client_id", [None])[0]
    if not code:
        raise ChatGPTAuthError("The sign-in callback did not contain an authorization code.")
    if issued_client_id and returned_client_id and returned_client_id != issued_client_id:
        raise ChatGPTAuthError("The callback used a different ChatGPT registration; credentials were not changed.")
    selected_client_id = issued_client_id or returned_client_id
    if not selected_client_id or selected_client_id == INITIAL_CLIENT_ID:
        raise ChatGPTAuthError("The callback did not return Nyabot's issued client ID; registration is incomplete.")

    if not issued_client_id:
        # Preserve the registration even if code exchange needs to be retried.
        metadata["client_id"] = selected_client_id
        with _refresh_lock, _credential_file_lock():
            _save_metadata(metadata)

    tokens = _form_request(
        f"{AUTH_BASE}/api/accounts/oauth/token",
        {
            "grant_type": "authorization_code",
            "client_id": selected_client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": API_RESOURCE,
        },
    )
    scopes = set(str(tokens.get("scope", "")).split())
    if not REQUIRED_SCOPES.issubset(scopes):
        missing = ", ".join(sorted(REQUIRED_SCOPES - scopes))
        raise ChatGPTAuthError(f"ChatGPT did not grant required plan access scopes: {missing}")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    id_token = tokens.get("id_token")
    if not access_token or not refresh_token or not id_token:
        raise ChatGPTAuthError("The sign-in response is missing required credentials; run login again.")

    oidc = _openid_configuration()
    claims = _verify_id_token(
        id_token,
        client_id=selected_client_id,
        nonce=nonce,
        config=oidc,
    )
    if metadata.get("subject") and metadata.get("subject") != claims.get("sub"):
        raise ChatGPTAuthError("The signed-in account differs from the saved Nyabot account.")

    # Keep bearer credentials in the OS credential manager, not in the project tree.
    updated = {
        "host_id": host_id,
        "client_id": selected_client_id,
        "connected": True,
        "subject": claims.get("sub"),
        "email": claims.get("email", metadata.get("email", "")),
        "issuer": claims.get("iss", AUTH_BASE),
        "scopes": sorted(scopes),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        "paused": False,
    }
    with _refresh_lock, _credential_file_lock():
        _set_credential("refresh_token", refresh_token)
        _set_credential("access_token", access_token)
        _save_metadata(updated)
    print(f"ChatGPT plan connected for {updated['email'] or 'the signed-in account'}.")


def get_valid_access_token() -> str:
    """Return an access token, refreshing it when it is near expiry."""
    with _refresh_lock, _credential_file_lock():
        metadata = _load_metadata()
        if not metadata.get("client_id"):
            raise ChatGPTAuthError("ChatGPT is not connected. Run: python -m services.chatgpt_auth login")
        if metadata.get("paused"):
            raise ChatGPTAuthError(
                "ChatGPT plan requests are paused after a usage limit. Check ChatGPT Settings > Usage, "
                "then run: python -m services.chatgpt_auth resume"
            )
        if "chatgpt.tokens.use.direct" not in metadata.get("scopes", []):
            raise ChatGPTAuthError("ChatGPT plan access is missing. Run: python -m services.chatgpt_auth login")

        access_token = _get_credential("access_token")
        refresh_token = _get_credential("refresh_token")
        if not refresh_token:
            raise ChatGPTAuthError("ChatGPT credentials are missing. Run: python -m services.chatgpt_auth login")
        if access_token and float(metadata.get("expires_at", 0)) > time.time() + 120:
            return access_token

        refreshed = _form_request(
            f"{AUTH_BASE}/api/accounts/oauth/token",
            {
                "grant_type": "refresh_token",
                "client_id": metadata["client_id"],
                "refresh_token": refresh_token,
                "resource": API_RESOURCE,
            },
        )
        new_access_token = refreshed.get("access_token")
        new_refresh_token = refreshed.get("refresh_token") or refresh_token
        if not new_access_token:
            raise ChatGPTAuthError("ChatGPT refresh response did not include an access token; run login again.")
        refreshed_scopes = set(str(refreshed.get("scope", "")).split()) or set(metadata.get("scopes", []))
        if "chatgpt.tokens.use.direct" not in refreshed_scopes:
            raise ChatGPTAuthError("ChatGPT plan access is no longer enabled. Run login again.")

        # Save refresh rotation before the short-lived access token.
        _set_credential("refresh_token", new_refresh_token)
        _set_credential("access_token", new_access_token)
        metadata["scopes"] = sorted(refreshed_scopes)
        metadata["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600))
        _save_metadata(metadata)
        return new_access_token


def mark_plan_paused() -> None:
    with _refresh_lock, _credential_file_lock():
        metadata = _load_metadata()
        if metadata:
            metadata["paused"] = True
            _save_metadata(metadata)


def clear_plan_pause() -> None:
    with _refresh_lock, _credential_file_lock():
        metadata = _load_metadata()
        if not metadata.get("client_id"):
            raise ChatGPTAuthError("ChatGPT is not connected. Run: python -m services.chatgpt_auth login")
        metadata["paused"] = False
        _save_metadata(metadata)


def show_status() -> None:
    metadata = _load_metadata()
    if not metadata.get("client_id"):
        print("ChatGPT: not connected")
        return
    has_refresh = bool(_get_credential("refresh_token"))
    state = "connected" if has_refresh and metadata.get("connected", True) else "signed out; run login"
    if metadata.get("connected", True) and not has_refresh:
        state = "credentials missing; run login"
    if metadata.get("paused"):
        state = "paused after usage limit"
    print(f"ChatGPT: {state}")
    print(f"Account: {metadata.get('email') or 'unknown'}")
    print(f"Plan access: {'enabled' if 'chatgpt.tokens.use.direct' in metadata.get('scopes', []) else 'missing'}")


def list_models() -> None:
    import asyncio

    from services.chatgpt_api import close_session, fetch_models

    async def _fetch_and_close():
        try:
            return await fetch_models()
        finally:
            await close_session()

    try:
        models = asyncio.run(_fetch_and_close())
    except Exception as exc:
        raise ChatGPTAuthError(str(exc)) from exc
    for model in models:
        print(f"{model.get('slug', '')}\t{model.get('display_name', '')}")


def logout() -> None:
    metadata = _load_metadata()
    refresh_token = _get_credential("refresh_token")
    remote_revoked = False
    revoke_error = None
    if refresh_token:
        try:
            oidc = _openid_configuration()
            revoke_endpoint = oidc.get("revocation_endpoint")
            if revoke_endpoint:
                body = urllib.parse.urlencode(
                    {
                        "client_id": metadata.get("client_id", ""),
                        "token": refresh_token,
                        "token_type_hint": "refresh_token",
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    revoke_endpoint,
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=15):
                    pass
                remote_revoked = True
        except ChatGPTAuthError as exc:
            revoke_error = str(exc)
        except Exception as exc:
            revoke_error = str(exc)
    with _refresh_lock, _credential_file_lock():
        _delete_credentials()
        if metadata.get("client_id"):
            metadata["connected"] = False
            metadata["paused"] = False
            _save_metadata(metadata)
        else:
            try:
                METADATA_FILE.unlink()
            except FileNotFoundError:
                pass
    print("Local ChatGPT credentials removed.")
    if refresh_token and not remote_revoked:
        if revoke_error:
            print(f"Remote revocation was not confirmed ({revoke_error}); disconnect Nyabot in ChatGPT Settings if needed.")
        else:
            print("Remote revocation was not confirmed; disconnect Nyabot in ChatGPT Settings if needed.")


def main() -> int:
    actions = {
        "login": login,
        "status": show_status,
        "models": list_models,
        "logout": logout,
        "resume": clear_plan_pause,
    }
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    action = actions.get(command)
    if not action:
        print("Usage: python -m services.chatgpt_auth [login|status|models|resume|logout]")
        return 2
    try:
        action()
    except ChatGPTAuthError as exc:
        print(f"[ChatGPT auth] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
