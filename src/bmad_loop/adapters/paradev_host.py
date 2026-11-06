"""Small authenticated loopback client; errors never contain response bodies.

Adapted with the owner's explicit incorporation direction from their
open-runtime/paradev-tooling source at a5d76d363da3cca58db5e110942c1490a49f8fae.
The original transport has no separate top-level license; the vendored BMAD
license does not license that transport. This is a maintained package-local port.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def discover_cli():
    """Prefer PATH; the installed app bundle link is the supported local fallback."""
    return (
        shutil.which("paradev")
        or shutil.which("superset")
        or str(Path.home() / ".superset/bin/superset")
    )


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class HostError(RuntimeError):
    """A public, credential-free failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise HostError("HOST_REDIRECT_REFUSED")


def _endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        if hostname == "localhost":
            hostname = "127.0.0.1"
        if not hostname or not ipaddress.ip_address(hostname).is_loopback:
            raise ValueError
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        authority = f"[{hostname}]" if ":" in hostname else hostname
        return "http://" + authority + (f":{port}" if port else "")
    except (AttributeError, TypeError, ValueError):
        raise HostError("HOST_ENDPOINT_NOT_LOOPBACK") from None


class Host:
    def __init__(self, manifest=None, cli_path=None, timeout=10):
        self.timeout = timeout
        self.cli_path = str(cli_path or discover_cli())
        try:
            if manifest is None:
                candidates = list((Path.home() / ".superset/host").glob("*/manifest.json"))
                if len(candidates) != 1:
                    raise HostError("HOST_MANIFEST_NOT_UNIQUE")
                manifest = candidates[0]
            if isinstance(manifest, (str, Path)):
                path = Path(manifest)
                stat = path.stat()
                if stat.st_size > 65536 or stat.st_uid != os.getuid() or stat.st_mode & 0o022:
                    raise HostError("HOST_MANIFEST_UNSAFE")
                manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise HostError("HOST_MANIFEST_INVALID")
            self.endpoint = _endpoint(manifest.get("endpoint"))
            token = manifest.get("authToken")
            if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
                raise HostError("HOST_AUTH_MISSING")
            self._token = token
        except HostError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            raise HostError("HOST_MANIFEST_INVALID") from None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._open_http = self._opener.open  # Binary HTTP response; not Path.open text I/O.

    def _call(self, path, procedure, payload, mutate, pattern, query):
        if not isinstance(procedure, str) or not re.fullmatch(pattern, procedure):
            raise HostError("HOST_PROCEDURE_INVALID")
        try:
            body = json.dumps(payload, allow_nan=False).encode()
            url = self.endpoint + path + procedure
            if not mutate and query:
                url += "?input=" + urllib.parse.quote(body.decode(), safe="")
            request = urllib.request.Request(
                url,
                data=body if mutate else None,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
            )
            with self._open_http(request, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise HostError("HOST_RESPONSE_TOO_LARGE")
            document = json.loads(data)
            if not isinstance(document, dict) or "error" in document:
                raise HostError("HOST_RPC_FAILED")
            return document["result"]
        except HostError:
            raise
        except urllib.error.HTTPError as error:
            raise HostError(f"HOST_HTTP_{error.code}") from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise HostError("HOST_UNAVAILABLE") from None
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise HostError("HOST_RESPONSE_INVALID") from None

    def rpc(self, procedure, payload=None, mutate=False):
        try:
            result = self._call(
                "/trpc/",
                procedure,
                {"json": payload},
                mutate,
                r"[A-Za-z]\w*(?:\.[A-Za-z]\w*)*",
                payload is not None,
            )["data"]
            return result["json"] if isinstance(result, dict) and "json" in result else result
        except HostError:
            raise
        except (KeyError, TypeError):
            raise HostError("HOST_RESPONSE_INVALID") from None

    def chat(self, procedure, payload=None, mutate=False):
        """The stock chat-v3 runtime router: plain JSON (no superjson envelope), un-namespaced procedures.

        A void mutation answers {"result": {}} and returns None. 409 means the chat is not live.
        """
        result = self._call(
            "/chat-v3/trpc/", procedure, payload, mutate, r"[A-Za-z]\w*", payload is not None
        )
        if not isinstance(result, dict):
            raise HostError("HOST_RESPONSE_INVALID")
        return result.get("data")

    def cli(self, args, *, cwd=None, env_overrides=None):
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or "\0" in arg for arg in args
        ):
            raise HostError("CLI_ARGUMENT_INVALID")
        command = [self.cli_path, *args]
        if "--json" not in args:
            command.append("--json")
        context = {}
        if cwd is not None:
            context["cwd"] = str(cwd)
        if env_overrides is not None:
            if (
                not isinstance(env_overrides, dict)
                or set(env_overrides) - {"SUPERSET_WORKSPACE_PATH", "SUPERSET_WORKSPACE_ID"}
                or any(
                    not isinstance(value, str) or "\0" in value for value in env_overrides.values()
                )
            ):
                raise HostError("CLI_CONTEXT_INVALID")
            # Do not mutate or export the caller's environment. The bundled Page
            # CLI computes its entry path from this explicit workspace context.
            context["env"] = {**os.environ, **env_overrides}
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                check=False,
                **context,
            )
            if result.returncode:
                raise HostError("CLI_FAILED")
            if len(result.stdout.encode()) > MAX_RESPONSE_BYTES:
                raise HostError("CLI_RESPONSE_TOO_LARGE")
            return json.loads(result.stdout)
        except HostError:
            raise
        except subprocess.TimeoutExpired:
            raise HostError("CLI_TIMEOUT") from None
        except (OSError, ValueError, TypeError, UnicodeError):
            raise HostError("CLI_RESPONSE_INVALID") from None
