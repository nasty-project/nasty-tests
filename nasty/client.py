import json
import sys

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' package required.  Install with: pip install websockets")
    sys.exit(1)


class NastyClient:
    def __init__(self, host: str, port: int = 443, password: str = "admin"):
        self.host = host
        self.port = port
        self.password = password
        self.ws = None
        self._id = 0
        self.token = None

    async def connect(self):
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        uri = f"wss://{self.host}:{self.port}/ws"
        self.ws = await websockets.connect(uri, ssl=ssl_ctx)

        await self._login()

        await self.ws.send(json.dumps({"token": self.token}))
        auth_resp = json.loads(await self.ws.recv())
        if not auth_resp.get("authenticated"):
            raise Exception(f"WebSocket auth failed: {auth_resp}")

    async def _login(self):
        import ssl
        import urllib.request

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        data = json.dumps({"username": "admin", "password": self.password}).encode()
        req = urllib.request.Request(
            f"https://{self.host}:{self.port}/api/login",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, context=ssl_ctx)
        self.token = json.loads(resp.read())["token"]

    async def call(self, method: str, params: dict = None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await self.ws.recv())
            if "id" not in resp:
                continue  # skip server-push event notifications
            if "error" in resp:
                raise Exception(f"RPC error ({method}): {resp['error']}")
            return resp.get("result")

    async def close(self):
        if self.ws:
            await self.ws.close()
