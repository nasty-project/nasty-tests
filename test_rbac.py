import secrets

from nasty.client import NastyClient, RpcError
from nasty.context import TestContext
from nasty.output import header


def _authorization_error(error: RpcError) -> bool:
    message = error.message.lower()
    return any(marker in message for marker in (
        "access denied", "permission denied", "not permitted", "outside scope",
        "not owned",
    ))


def _share_matches(protocol: str, share: dict, expected: str) -> bool:
    if protocol == "nfs":
        return share.get("path") == expected
    if protocol == "smb":
        return share.get("name") == expected
    if protocol == "iscsi":
        return share.get("iqn") == f"iqn.2137-04.storage.nasty:{expected}"
    return share.get("nqn") == f"nqn.2137-04.storage.nasty:{expected}"


async def _reconcile_share(client: NastyClient, protocol: str,
                           expected: str) -> str | None:
    try:
        shares = await client.call(f"share.{protocol}.list")
        for share in shares:
            if _share_matches(protocol, share, expected):
                try:
                    await client.call(f"share.{protocol}.delete",
                                      {"id": share["id"]})
                except Exception:
                    pass
        shares = await client.call(f"share.{protocol}.list")
        if any(_share_matches(protocol, share, expected) for share in shares):
            return f"share still exists: {expected}"
        return None
    except Exception as error:
        return str(error)


async def _denied(client: NastyClient, method: str, params: dict | None = None):
    try:
        await client.call(method, params)
        return False, "request unexpectedly succeeded"
    except RpcError as error:
        return _authorization_error(error), error.message


async def test_rbac(ctx: TestContext):
    header("RBAC and Scoped Token Tests")
    password = secrets.token_urlsafe(18)
    operator_user = f"testop-{ctx.tag}"
    readonly_user = f"testro-{ctx.tag}"
    token_a_name = f"test-token-a-{ctx.tag}"
    token_b_name = f"test-token-b-{ctx.tag}"
    readonly_token_name = f"test-token-ro-{ctx.tag}"
    sentinel_name = f"test-rbac-admin-{ctx.tag}"
    owned_a_name = f"test-rbac-a-{ctx.tag}"
    owned_b_name = f"test-rbac-b-{ctx.tag}"
    orphan_snapshot_name = f"test-rbac-orphan-snap-{ctx.tag}"
    orphan_clone_name = f"test-rbac-orphan-clone-{ctx.tag}"
    foreign_clone_name = f"test-rbac-foreign-clone-{ctx.tag}"

    clients = []
    token_ids = []
    users_created = [operator_user, readonly_user]
    shares_created = []
    share_fixtures = []

    try:
        admin_me = await ctx.client.call("auth.me")
        ctx.record("RBAC: admin identity", admin_me.get("role") == "admin",
                   str(admin_me))

        for username, role in [(operator_user, "operator"),
                               (readonly_user, "readonly")]:
            await ctx.client.call("auth.create_user", {
                "username": username,
                "password": password,
                "role": role,
            })

        operator = NastyClient(ctx.host, ctx.client.port, password,
                               username=operator_user)
        readonly = NastyClient(ctx.host, ctx.client.port, password,
                               username=readonly_user)
        for client in (operator, readonly):
            clients.append(client)
            await client.connect()

        operator_me = await operator.call("auth.me")
        ctx.record("RBAC: operator interactive login",
                   operator_me.get("role") == "operator", str(operator_me))
        readonly_me = await readonly.call("auth.me")
        ctx.record("RBAC: readonly interactive login",
                   readonly_me.get("role") == "readonly", str(readonly_me))

        denied, detail = await _denied(operator, "auth.token.list")
        ctx.record("RBAC: operator denied token administration", denied, detail)
        denied, detail = await _denied(readonly, "subvolume.create", {
            "filesystem": ctx.pool,
            "name": f"test-rbac-denied-{ctx.tag}",
            "subvolume_type": "filesystem",
            "volsize_bytes": 16 * 1024 * 1024,
        })
        ctx.record("RBAC: readonly denied mutation", denied, detail)

        operator_filesystems = await operator.call("fs.list")
        ctx.record("RBAC: operator can read filesystems",
                   any(fs.get("name") == ctx.pool for fs in operator_filesystems))
        readonly_filesystems = await readonly.call("fs.list")
        ctx.record("RBAC: readonly can read filesystems",
                   any(fs.get("name") == ctx.pool for fs in readonly_filesystems))

        token_specs = [
            (token_a_name, "operator"),
            (token_b_name, "operator"),
            (readonly_token_name, "readonly"),
        ]
        token_clients = {}
        for name, role in token_specs:
            token = await ctx.client.call("auth.token.create", {
                "name": name,
                "role": role,
                "filesystem": ctx.pool,
                "expires_in_secs": 3600,
            })
            token_ids.append(token["id"])
            client = NastyClient(ctx.host, ctx.client.port, password=None,
                                 token=token["token"])
            clients.append(client)
            await client.connect()
            token_clients[name] = client

        token_a = token_clients[token_a_name]
        token_b = token_clients[token_b_name]
        readonly_token = token_clients[readonly_token_name]

        for label, client, role in [
            ("operator token A", token_a, "operator"),
            ("operator token B", token_b, "operator"),
            ("readonly token", readonly_token, "readonly"),
        ]:
            me = await client.call("auth.me")
            ctx.record(f"RBAC: {label} is scoped",
                       me.get("role") == role and me.get("scoped") is True,
                       str(me))

        token_a_filesystems = await token_a.call("fs.list")
        ctx.record("RBAC: operator token filesystem scope",
                   [fs.get("name") for fs in token_a_filesystems] == [ctx.pool],
                   str(token_a_filesystems))
        readonly_token_filesystems = await readonly_token.call("fs.list")
        ctx.record("RBAC: readonly token filesystem scope",
                   [fs.get("name") for fs in readonly_token_filesystems] == [ctx.pool],
                   str(readonly_token_filesystems))

        denied, detail = await _denied(token_a, "fs.get", {
            "name": f"outside-scope-{ctx.tag}",
        })
        ctx.record("RBAC: scoped token denied outside filesystem", denied, detail)

        sentinel = await ctx.client.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": sentinel_name,
            "subvolume_type": "filesystem",
            "volsize_bytes": 16 * 1024 * 1024,
        })
        ctx.record("RBAC: admin sentinel created", sentinel.get("created") is True)

        share_fixtures.append(("nfs", sentinel["path"]))
        try:
            escaped_share = await token_a.call("share.nfs.create", {
                "path": sentinel["path"],
                "clients": [{
                    "host": "*",
                    "options": "ro,sync,no_subtree_check,root_squash",
                }],
            })
            shares_created.append(("nfs", escaped_share["id"]))
            share_denied = False
            share_detail = "scoped token exported an admin-owned subvolume"
        except RpcError as error:
            share_denied = _authorization_error(error)
            share_detail = error.message
        ctx.record("RBAC: scoped token denied foreign share creation",
                   share_denied, share_detail)

        foreign_smb_name = f"test-rbac-foreign-{ctx.tag}"
        share_fixtures.append(("smb", foreign_smb_name))
        try:
            escaped_smb = await token_a.call("share.smb.create", {
                "name": foreign_smb_name,
                "path": sentinel["path"],
                "guest_ok": False,
            })
            shares_created.append(("smb", escaped_smb["id"]))
            smb_denied = False
            smb_detail = "scoped token created an SMB share for an admin-owned subvolume"
        except RpcError as error:
            smb_denied = _authorization_error(error)
            smb_detail = error.message
        ctx.record("RBAC: scoped token denied foreign SMB share",
                   smb_denied, smb_detail)

        block_name = f"test-rbac-block-{ctx.tag}"
        block = await ctx.client.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": block_name,
            "subvolume_type": "block",
            "volsize_bytes": 64 * 1024 * 1024,
        })

        foreign_iscsi_name = f"test-rbac-foreign-iscsi-{ctx.tag}"
        share_fixtures.append(("iscsi", foreign_iscsi_name))
        try:
            escaped_iscsi = await token_a.call("share.iscsi.create", {
                "name": foreign_iscsi_name,
                "device_path": block["block_device"],
            })
            shares_created.append(("iscsi", escaped_iscsi["id"]))
            iscsi_denied = False
            iscsi_detail = "scoped token exported an admin-owned block volume via iSCSI"
        except RpcError as error:
            iscsi_denied = _authorization_error(error)
            iscsi_detail = error.message
        ctx.record("RBAC: scoped token denied foreign iSCSI export",
                   iscsi_denied, iscsi_detail)
        if shares_created and shares_created[-1][0] == "iscsi":
            _, share_id = shares_created[-1]
            await ctx.client.call("share.iscsi.delete", {"id": share_id})
            shares_created.pop()

        foreign_nvme_name = f"test-rbac-foreign-nvme-{ctx.tag}"
        share_fixtures.append(("nvmeof", foreign_nvme_name))
        try:
            escaped_nvme = await token_a.call("share.nvmeof.create", {
                "name": foreign_nvme_name,
                "device_path": block["block_device"],
            })
            shares_created.append(("nvmeof", escaped_nvme["id"]))
            nvme_denied = False
            nvme_detail = "scoped token exported an admin-owned block volume via NVMe-oF"
        except RpcError as error:
            nvme_denied = _authorization_error(error)
            nvme_detail = error.message
        ctx.record("RBAC: scoped token denied foreign NVMe-oF export",
                   nvme_denied, nvme_detail)

        owned_a = await token_a.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": owned_a_name,
            "subvolume_type": "filesystem",
            "volsize_bytes": 16 * 1024 * 1024,
        })
        ctx.record("RBAC: token A stamps owner",
                   owned_a.get("created") is True and
                   owned_a.get("owner") == token_a_name, str(owned_a))

        owned_b = await token_b.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": owned_b_name,
            "subvolume_type": "filesystem",
            "volsize_bytes": 16 * 1024 * 1024,
        })
        ctx.record("RBAC: token B stamps owner",
                   owned_b.get("created") is True and
                   owned_b.get("owner") == token_b_name, str(owned_b))

        visible_a = {sv["name"] for sv in await token_a.call(
            "subvolume.list", {"filesystem": ctx.pool})}
        ctx.record("RBAC: token A owner isolation",
                   owned_a_name in visible_a and owned_b_name not in visible_a and
                   sentinel_name not in visible_a, str(sorted(visible_a)))
        visible_b = {sv["name"] for sv in await token_b.call(
            "subvolume.list", {"filesystem": ctx.pool})}
        ctx.record("RBAC: token B owner isolation",
                   owned_b_name in visible_b and owned_a_name not in visible_b and
                   sentinel_name not in visible_b, str(sorted(visible_b)))

        denied, detail = await _denied(token_a, "subvolume.get", {
            "filesystem": ctx.pool,
            "name": owned_b_name,
        })
        ctx.record("RBAC: token A denied token B subvolume", denied, detail)

        readonly_visible = {sv["name"] for sv in await readonly_token.call(
            "subvolume.list", {"filesystem": ctx.pool})}
        ctx.record("RBAC: scoped readonly sees filesystem resources",
                   {sentinel_name, owned_a_name, owned_b_name} <= readonly_visible,
                   str(sorted(readonly_visible)))

        await token_a.call("snapshot.create", {
            "filesystem": ctx.pool,
            "subvolume": owned_a_name,
            "name": orphan_snapshot_name,
            "read_only": True,
        })
        ctx.record("RBAC: token A creates owned snapshot", True)

        await token_a.call("subvolume.delete", {
            "filesystem": ctx.pool, "name": owned_a_name,
        })
        ctx.record("RBAC: token A deletes owned subvolume", True)

        await token_b.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": owned_a_name,
            "subvolume_type": "filesystem",
            "volsize_bytes": 16 * 1024 * 1024,
        })
        denied, detail = await _denied(token_a, "snapshot.clone", {
            "filesystem": ctx.pool,
            "subvolume": owned_a_name,
            "snapshot": orphan_snapshot_name,
            "new_name": foreign_clone_name,
        })
        ctx.record("RBAC: foreign recreated parent blocks orphan clone", denied, detail)
        await token_b.call("subvolume.delete", {
            "filesystem": ctx.pool, "name": owned_a_name,
        })

        orphan_clone = await token_a.call("snapshot.clone", {
            "filesystem": ctx.pool,
            "subvolume": owned_a_name,
            "snapshot": orphan_snapshot_name,
            "new_name": orphan_clone_name,
        })
        ctx.record("RBAC: token A clones owned orphan snapshot",
                   orphan_clone.get("owner") == token_a_name,
                   str(orphan_clone))

        denied, detail = await _denied(token_b, "snapshot.clone", {
            "filesystem": ctx.pool,
            "subvolume": owned_a_name,
            "snapshot": orphan_snapshot_name,
            "new_name": foreign_clone_name,
        })
        ctx.record("RBAC: token B denied foreign orphan clone", denied, detail)

        denied, detail = await _denied(token_b, "snapshot.delete", {
            "filesystem": ctx.pool,
            "subvolume": owned_a_name,
            "name": orphan_snapshot_name,
        })
        ctx.record("RBAC: token B denied foreign orphan delete", denied, detail)

        await token_a.call("snapshot.delete", {
            "filesystem": ctx.pool,
            "subvolume": owned_a_name,
            "name": orphan_snapshot_name,
        })
        ctx.record("RBAC: token A deletes owned orphan snapshot", True)

        await token_b.call("subvolume.delete", {
            "filesystem": ctx.pool, "name": owned_b_name,
        })
        ctx.record("RBAC: token B deletes owned subvolume", True)
    except Exception as error:
        ctx.record("RBAC: test", False, str(error))
    finally:
        for client in clients:
            try:
                await client.close()
            except Exception:
                pass
        cleanup_errors = []
        for protocol, share_id in reversed(shares_created):
            try:
                await ctx.client.call(f"share.{protocol}.delete", {"id": share_id})
            except Exception:
                pass
        for protocol, expected in share_fixtures:
            error = await _reconcile_share(ctx.client, protocol, expected)
            if error:
                cleanup_errors.append(f"{protocol} share {expected}: {error}")
        try:
            await ctx.client.call("snapshot.delete", {
                "filesystem": ctx.pool,
                "subvolume": owned_a_name,
                "name": orphan_snapshot_name,
            })
        except Exception:
            pass
        expected_subvolumes = {
            sentinel_name, owned_a_name, owned_b_name,
            f"test-rbac-block-{ctx.tag}",
            orphan_clone_name, foreign_clone_name,
        }
        for name in expected_subvolumes:
            try:
                await ctx.client.call("subvolume.detach", {
                    "filesystem": ctx.pool, "name": name,
                })
            except Exception:
                pass
            try:
                await ctx.client.call("subvolume.delete", {
                    "filesystem": ctx.pool, "name": name,
                })
            except Exception:
                pass
        try:
            remaining = await ctx.client.call(
                "subvolume.list", {"filesystem": ctx.pool})
            residual = [subvolume["name"] for subvolume in remaining
                        if subvolume.get("name") in expected_subvolumes]
            if residual:
                cleanup_errors.append(f"residual subvolumes: {residual}")
        except Exception as error:
            cleanup_errors.append(f"subvolume verification: {error}")
        for token_id in reversed(token_ids):
            try:
                await ctx.client.call("auth.token.delete", {"id": token_id})
            except Exception:
                pass
        for username in reversed(users_created):
            try:
                await ctx.client.call("auth.delete_user", {"username": username})
            except Exception:
                pass
        try:
            remaining_tokens = await ctx.client.call("auth.token.list")
            expected_token_names = {token_a_name, token_b_name,
                                    readonly_token_name}
            residual_tokens = [token for token in remaining_tokens
                               if token.get("name") in expected_token_names]
            for token in residual_tokens:
                await ctx.client.call("auth.token.delete", {"id": token["id"]})
            remaining_users = await ctx.client.call("auth.list_users")
            residual_users = [user["username"] for user in remaining_users
                              if user.get("username") in users_created]
            for username in residual_users:
                await ctx.client.call("auth.delete_user", {"username": username})

            remaining_tokens = await ctx.client.call("auth.token.list")
            residual_tokens = [token["name"] for token in remaining_tokens
                               if token.get("name") in expected_token_names]
            remaining_users = await ctx.client.call("auth.list_users")
            residual_users = [user["username"] for user in remaining_users
                              if user.get("username") in users_created]
            if residual_tokens:
                cleanup_errors.append(f"residual tokens: {residual_tokens}")
            if residual_users:
                cleanup_errors.append(f"residual users: {residual_users}")
        except Exception as error:
            cleanup_errors.append(f"credential verification: {error}")
        if cleanup_errors:
            ctx.record("RBAC: credential cleanup", False, "; ".join(cleanup_errors))
