import uuid

from .output import ok, fail


class TestContext:
    def __init__(self, client, host: str, pool: str, skip_delete: bool = False,
                 tag: str | None = None, remount: bool = False):
        self.client = client
        self.host = host
        self.pool = pool
        self.remount = remount
        self.skip_delete = skip_delete or remount
        self.tag = tag or uuid.uuid4().hex[:6]
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, passed: bool, detail: str = ""):
        self.results.append((name, passed, detail))
        if passed:
            ok(name)
        else:
            fail(f"{name}: {detail}")
