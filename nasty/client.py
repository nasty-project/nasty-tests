import json
import sys
import asyncio

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' package required.  Install with: pip install websockets")
    sys.exit(1)


class AuthenticationError(Exception):
    pass


class RpcError(Exception):
    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        self.code = error.get("code")
        self.message = error.get("message", str(error))
        super().__init__(f"RPC error ({method}): {error}")


class NastyClient:
    def __init__(self, host: str, port: int = 443, password: str | None = "admin",
                 username: str = "admin", token: str | None = None,
                 timeout: float = 30):
        self.host = host
        self.port = port
        self.password = password
        self.username = username
        self.timeout = timeout
        self.ws = None
        self._id = 0
        self.token = token
        self.auth_info = None
        self._call_lock = asyncio.Lock()

    async def connect(self):
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        uri = f"wss://{self.host}:{self.port}/ws"
        self.ws = await asyncio.wait_for(
            websockets.connect(uri, ssl=ssl_ctx), timeout=self.timeout)

        if self.token is None:
            await self._login()

        await asyncio.wait_for(
            self.ws.send(json.dumps({"token": self.token})), timeout=self.timeout)
        auth_resp = json.loads(await asyncio.wait_for(
            self.ws.recv(), timeout=self.timeout))
        if not auth_resp.get("authenticated"):
            raise AuthenticationError(f"WebSocket auth failed: {auth_resp}")
        self.auth_info = auth_resp

    async def _login(self):
        self.token = await asyncio.wait_for(
            asyncio.to_thread(self._login_sync), timeout=self.timeout)

    def _login_sync(self):
        import ssl
        import urllib.request

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        data = json.dumps({"username": self.username, "password": self.password}).encode()
        req = urllib.request.Request(
            f"https://{self.host}:{self.port}/api/login",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=self.timeout)
        except Exception as e:
            raise AuthenticationError(f"Login failed for {self.username}: {e}") from e
        return json.loads(resp.read())["token"]

    async def call(self, method: str, params: dict = None):
        async with self._call_lock:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            self._id += 1
            request_id = self._id
            msg = {"jsonrpc": "2.0", "method": method, "id": request_id}
            if params:
                msg["params"] = params
            await asyncio.wait_for(
                self.ws.send(json.dumps(msg)), timeout=self.timeout)
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"RPC call timed out: {method}")
                resp = json.loads(await asyncio.wait_for(
                    self.ws.recv(), timeout=remaining))
                if resp.get("id") != request_id:
                    continue  # skip server-push event notifications
                if "error" in resp:
                    raise RpcError(method, resp["error"])
                return resp.get("result")

    async def reconnect(self):
        await self.close()
        await self.connect()

    async def close(self):
        if self.ws:
            try:
                await asyncio.wait_for(self.ws.close(), timeout=self.timeout)
            finally:
                self.ws = None
