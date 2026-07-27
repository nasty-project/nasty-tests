import os
import secrets

from nasty.context import TestContext
from nasty.output import header
from nasty.shell import cleanup_mount, run


VOLUME_BYTES = 64 * 1024 * 1024


def _new_mountpoint(path: str) -> str | None:
    if not path.startswith("/tmp/nasty-test-") or os.path.lexists(path):
        return f"refusing unsafe or existing mountpoint: {path}"
    os.mkdir(path, mode=0o700)
    return None


def _credentials_file(path: str, username: str, password: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as credentials:
            credentials.write(f"username={username}\npassword={password}\n")
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _share_matches(protocol: str, share: dict, expected: str) -> bool:
    if protocol == "nfs":
        return share.get("path") == expected
    if protocol == "smb":
        return share.get("name") == expected
    if protocol == "iscsi":
        return share.get("iqn") == f"iqn.2137-04.storage.nasty:{expected}"
    return share.get("nqn") == f"nqn.2137-04.storage.nasty:{expected}"


async def _reconcile_share(ctx: TestContext, protocol: str,
                           expected: str) -> str | None:
    try:
        shares = await ctx.client.call(f"share.{protocol}.list")
        for share in shares:
            if _share_matches(protocol, share, expected):
                try:
                    await ctx.client.call(f"share.{protocol}.delete",
                                          {"id": share["id"]})
                except Exception:
                    pass
        shares = await ctx.client.call(f"share.{protocol}.list")
        if any(_share_matches(protocol, share, expected) for share in shares):
            return f"share still exists: {expected}"
        return None
    except Exception as error:
        return str(error)


async def test_nfs_auth(ctx: TestContext):
    header("NFS Client Authorization")
    name = f"test-auth-nfs-{ctx.tag}"
    mountpoint = f"/tmp/nasty-test-auth-nfs-{ctx.tag}"
    share_id = None
    share_path = None
    mounted = False

    try:
        subvolume = await ctx.client.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": name,
            "subvolume_type": "filesystem",
            "volsize_bytes": VOLUME_BYTES,
        })
        share_path = subvolume["path"]
        share = await ctx.client.call("share.nfs.create", {
            "path": subvolume["path"],
            "clients": [{
                "host": "*",
                "options": "rw,sync,no_subtree_check,no_root_squash",
            }],
        })
        share_id = share["id"]
        ctx.record("NFS auth: allowed export created", True)

        mountpoint_error = _new_mountpoint(mountpoint)
        result = (run(["mount", "-t", "nfs4", f"{ctx.host}:{subvolume['path']}",
                       mountpoint], check=False)
                  if mountpoint_error is None else None)
        mounted = result is not None and result.returncode == 0
        ctx.record("NFS auth: allowed client mounts", mounted,
                   mountpoint_error or (result.stderr.strip() if result else ""))

        marker_ok = False
        detail = "mount failed"
        if mounted:
            try:
                marker = os.path.join(mountpoint, "auth-marker.txt")
                with open(marker, "w") as marker_file:
                    marker_file.write(ctx.tag)
                with open(marker) as marker_file:
                    marker_ok = marker_file.read() == ctx.tag
                detail = ""
            except Exception as error:
                detail = str(error)
        ctx.record("NFS auth: allowed client read/write", marker_ok, detail)

        positive_mount = mounted
        cleanup_error = cleanup_mount(mountpoint, mounted)
        if cleanup_error:
            raise RuntimeError(cleanup_error)
        mounted = False

        if not positive_mount:
            ctx.record("NFS auth: unauthorized client denied", False,
                       "positive mount prerequisite failed")
            return

        await ctx.client.call("share.nfs.update", {
            "id": share_id,
            "clients": [{
                "host": "192.0.2.1",
                "options": "rw,sync,no_subtree_check,no_root_squash",
            }],
        })
        mountpoint_error = _new_mountpoint(mountpoint)
        denied = (run(["mount", "-t", "nfs4", f"{ctx.host}:{subvolume['path']}",
                       mountpoint], check=False)
                  if mountpoint_error is None else None)
        denied_ok = denied is not None and denied.returncode != 0
        detail = mountpoint_error or (
            "mount unexpectedly succeeded" if not denied_ok else "")
        if denied is not None and denied.returncode == 0:
            mounted = True
            cleanup_error = cleanup_mount(mountpoint, True)
            if cleanup_error:
                raise RuntimeError(cleanup_error)
            mounted = False
        elif os.path.isdir(mountpoint):
            os.rmdir(mountpoint)

        await ctx.client.call("share.nfs.update", {
            "id": share_id,
            "clients": [{
                "host": "*",
                "options": "rw,sync,no_subtree_check,no_root_squash",
            }],
        })
        restore_error = _new_mountpoint(mountpoint)
        restored = (run(["mount", "-t", "nfs4",
                         f"{ctx.host}:{subvolume['path']}", mountpoint], check=False)
                    if restore_error is None else None)
        restored_ok = restored is not None and restored.returncode == 0
        if restored_ok:
            mounted = True
            cleanup_error = cleanup_mount(mountpoint, True)
            if cleanup_error:
                raise RuntimeError(cleanup_error)
            mounted = False
        elif os.path.isdir(mountpoint):
            os.rmdir(mountpoint)
        if denied_ok and not restored_ok:
            detail = restore_error or "access was not restored after the denial"
        ctx.record("NFS auth: unauthorized client denied",
                   denied_ok and restored_ok, detail)
    except Exception as error:
        ctx.record("NFS auth: test", False, str(error))
    finally:
        cleanup_error = cleanup_mount(mountpoint, mounted)
        if cleanup_error:
            ctx.record("NFS auth: mount cleanup", False, cleanup_error)
        if share_id:
            try:
                await ctx.client.call("share.nfs.delete", {"id": share_id})
            except Exception:
                pass
        if share_path:
            cleanup_error = await _reconcile_share(ctx, "nfs", share_path)
            if cleanup_error:
                ctx.record("NFS auth: share cleanup", False, cleanup_error)
        try:
            await ctx.client.call("subvolume.delete", {
                "filesystem": ctx.pool, "name": name,
            })
        except Exception:
            pass


async def test_smb_auth(ctx: TestContext):
    header("SMB Authentication and Share ACLs")
    name = f"test-auth-smb-{ctx.tag}"
    allowed_user = f"test-smb-ok-{ctx.tag}"
    denied_user = f"test-smb-no-{ctx.tag}"
    password = secrets.token_urlsafe(18)
    mountpoint = f"/tmp/nasty-test-auth-smb-{ctx.tag}"
    share_id = None
    mounted = False
    users = [allowed_user, denied_user]
    credential_paths = []

    try:
        for username in (allowed_user, denied_user):
            await ctx.client.call("smb.user.create", {
                "username": username, "password": password,
            })
        ctx.record("SMB auth: users created", True)

        subvolume = await ctx.client.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": name,
            "subvolume_type": "filesystem",
            "volsize_bytes": VOLUME_BYTES,
        })
        share = await ctx.client.call("share.smb.create", {
            "name": name,
            "path": subvolume["path"],
            "guest_ok": False,
            "browseable": True,
            "valid_users": [allowed_user],
        })
        share_id = share["id"]
        ctx.record("SMB auth: restricted share created",
                   share.get("guest_ok") is False and
                   share.get("valid_users") == [allowed_user], str(share))

        allowed_credentials = f"/tmp/nasty-test-smb-creds-ok-{ctx.tag}"
        _credentials_file(allowed_credentials, allowed_user, password)
        credential_paths.append(allowed_credentials)
        mountpoint_error = _new_mountpoint(mountpoint)
        result = (run([
            "mount", "-t", "cifs", f"//{ctx.host}/{name}", mountpoint,
            "-o", f"credentials={allowed_credentials},vers=3.0,nosharesock",
        ], check=False) if mountpoint_error is None else None)
        mounted = result is not None and result.returncode == 0
        ctx.record("SMB auth: allowed user mounts", mounted,
                   mountpoint_error or (result.stderr.strip() if result else ""))

        marker_ok = False
        detail = "mount failed"
        if mounted:
            try:
                marker = os.path.join(mountpoint, "auth-marker.txt")
                with open(marker, "w") as marker_file:
                    marker_file.write(ctx.tag)
                with open(marker) as marker_file:
                    marker_ok = marker_file.read() == ctx.tag
                detail = ""
            except Exception as error:
                detail = str(error)
        ctx.record("SMB auth: allowed user read/write", marker_ok, detail)

        positive_mount = mounted
        cleanup_error = cleanup_mount(mountpoint, mounted)
        if cleanup_error:
            raise RuntimeError(cleanup_error)
        mounted = False


        if not positive_mount:
            prerequisite = "positive authenticated mount prerequisite failed"
            ctx.record("SMB auth: wrong password denied", False, prerequisite)
            ctx.record("SMB auth: disallowed user denied", False, prerequisite)
            return

        wrong_credentials = f"/tmp/nasty-test-smb-creds-wrong-{ctx.tag}"
        _credentials_file(wrong_credentials, allowed_user, secrets.token_urlsafe(18))
        credential_paths.append(wrong_credentials)
        mountpoint_error = _new_mountpoint(mountpoint)
        wrong = (run([
            "mount", "-t", "cifs", f"//{ctx.host}/{name}", mountpoint,
            "-o", f"credentials={wrong_credentials},vers=3.0,nosharesock",
        ], check=False) if mountpoint_error is None else None)
        wrong_denied = wrong is not None and wrong.returncode != 0
        detail = mountpoint_error or (
            "mount unexpectedly succeeded" if not wrong_denied else "")
        if wrong is not None and wrong.returncode == 0:
            mounted = True
            cleanup_error = cleanup_mount(mountpoint, True)
            if cleanup_error:
                raise RuntimeError(cleanup_error)
            mounted = False
        elif os.path.isdir(mountpoint):
            os.rmdir(mountpoint)

        mountpoint_error = _new_mountpoint(mountpoint)
        restored = (run([
            "mount", "-t", "cifs", f"//{ctx.host}/{name}", mountpoint,
            "-o", f"credentials={allowed_credentials},vers=3.0,nosharesock",
        ], check=False) if mountpoint_error is None else None)
        restored_ok = restored is not None and restored.returncode == 0
        if restored_ok:
            mounted = True
            cleanup_error = cleanup_mount(mountpoint, True)
            if cleanup_error:
                raise RuntimeError(cleanup_error)
            mounted = False
        elif os.path.isdir(mountpoint):
            os.rmdir(mountpoint)
        if wrong_denied and not restored_ok:
            detail = mountpoint_error or "valid credentials were not restored"
        ctx.record("SMB auth: wrong password denied",
                   wrong_denied and restored_ok, detail)

        denied_credentials = f"/tmp/nasty-test-smb-creds-denied-{ctx.tag}"
        _credentials_file(denied_credentials, denied_user, password)
        credential_paths.append(denied_credentials)
        mountpoint_error = _new_mountpoint(mountpoint)
        denied = (run([
            "mount", "-t", "cifs", f"//{ctx.host}/{name}", mountpoint,
            "-o", f"credentials={denied_credentials},vers=3.0,nosharesock",
        ], check=False) if mountpoint_error is None else None)
        user_denied = denied is not None and denied.returncode != 0
        detail = mountpoint_error or (
            "mount unexpectedly succeeded" if not user_denied else "")
        if denied is not None and denied.returncode == 0:
            mounted = True
            cleanup_error = cleanup_mount(mountpoint, True)
            if cleanup_error:
                raise RuntimeError(cleanup_error)
            mounted = False
        elif os.path.isdir(mountpoint):
            os.rmdir(mountpoint)

        mountpoint_error = _new_mountpoint(mountpoint)
        restored = (run([
            "mount", "-t", "cifs", f"//{ctx.host}/{name}", mountpoint,
            "-o", f"credentials={allowed_credentials},vers=3.0,nosharesock",
        ], check=False) if mountpoint_error is None else None)
        restored_ok = restored is not None and restored.returncode == 0
        if restored_ok:
            mounted = True
            cleanup_error = cleanup_mount(mountpoint, True)
            if cleanup_error:
                raise RuntimeError(cleanup_error)
            mounted = False
        elif os.path.isdir(mountpoint):
            os.rmdir(mountpoint)
        if user_denied and not restored_ok:
            detail = mountpoint_error or "allowed user access was not restored"
        ctx.record("SMB auth: disallowed user denied",
                   user_denied and restored_ok, detail)
    except Exception as error:
        ctx.record("SMB auth: test", False, str(error))
    finally:
        cleanup_error = cleanup_mount(mountpoint, mounted)
        if cleanup_error:
            ctx.record("SMB auth: mount cleanup", False, cleanup_error)
        for path in credential_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        if share_id:
            try:
                await ctx.client.call("share.smb.delete", {"id": share_id})
            except Exception:
                pass
        cleanup_error = await _reconcile_share(ctx, "smb", name)
        if cleanup_error:
            ctx.record("SMB auth: share cleanup", False, cleanup_error)
        try:
            await ctx.client.call("subvolume.delete", {
                "filesystem": ctx.pool, "name": name,
            })
        except Exception:
            pass
        cleanup_errors = []
        for username in reversed(users):
            try:
                await ctx.client.call("smb.user.delete", {"username": username})
            except Exception as error:
                cleanup_errors.append(f"{username}: {error}")
        try:
            remaining = await ctx.client.call("smb.user.list")
            residual = [user["username"] for user in remaining
                        if user.get("username") in users]
            if residual:
                cleanup_errors.append(f"residual users: {residual}")
        except Exception as error:
            cleanup_errors.append(f"verification: {error}")
        if cleanup_errors:
            ctx.record("SMB auth: credential cleanup", False,
                       "; ".join(cleanup_errors))


def _set_chap(iqn: str, host: str, username: str, password: str) -> str | None:
    settings = [
        ("node.session.auth.authmethod", "CHAP"),
        ("node.session.auth.username", username),
        ("node.session.auth.password", password),
    ]
    for key, value in settings:
        result = run([
            "iscsiadm", "-m", "node", "-T", iqn, "-p", f"{host}:3260",
            "--op", "update", "-n", key, "-v", value,
        ], check=False)
        if result.returncode != 0:
            return result.stderr.strip()
    return None


async def test_iscsi_auth(ctx: TestContext):
    header("iSCSI CHAP and Initiator ACLs")
    name = f"test-auth-iscsi-{ctx.tag}"
    target_id = None
    iqn = None
    logged_in = False

    try:
        with open("/etc/iscsi/initiatorname.iscsi") as initiator_file:
            initiator_iqn = next(line.split("=", 1)[1].strip()
                                 for line in initiator_file
                                 if line.startswith("InitiatorName="))
        chap_user = f"chap-{ctx.tag}"
        chap_password = secrets.token_hex(7)

        subvolume = await ctx.client.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": name,
            "subvolume_type": "block",
            "volsize_bytes": VOLUME_BYTES,
        })
        ctx.record("iSCSI auth: block volume created",
                   subvolume.get("created") is True)

        created_target = await ctx.client.call("share.iscsi.create", {
            "name": name,
            "device_path": subvolume["block_device"],
            "acls": [{
                "initiator_iqn": initiator_iqn,
                "userid": chap_user,
                "password": chap_password,
            }],
        })
        target_id = created_target["id"]
        iqn = created_target["iqn"]
        fetched_target = await ctx.client.call("share.iscsi.get", {"id": target_id})
        redacted = all(
            len(target.get("acls", [])) == 1 and
            target["acls"][0].get("password") == "***" and
            "password_encrypted" not in target["acls"][0]
            for target in (created_target, fetched_target)
        )
        ctx.record("iSCSI auth: target ACL secret redacted", redacted,
                   "ACL response exposed secret material" if not redacted else "")

        discovery = run([
            "iscsiadm", "-m", "discovery", "-t", "sendtargets", "-p", ctx.host,
        ], check=False)
        chap_error = _set_chap(iqn, ctx.host, chap_user, chap_password)
        login = (run([
            "iscsiadm", "-m", "node", "-T", iqn,
            "-p", f"{ctx.host}:3260", "--login",
        ], check=False) if discovery.returncode == 0 and chap_error is None else None)
        logged_in = login is not None and login.returncode == 0
        positive_login = logged_in
        ctx.record("iSCSI auth: correct CHAP accepted", logged_in,
                    chap_error or (login.stderr.strip() if login else discovery.stderr.strip()))

        if logged_in:
            logout = run([
                "iscsiadm", "-m", "node", "-T", iqn,
                "-p", f"{ctx.host}:3260", "--logout",
            ], check=False)
            if logout.returncode != 0:
                raise RuntimeError(f"iSCSI logout: {logout.stderr.strip()}")
            logged_in = False

        if not positive_login:
            ctx.record("iSCSI auth: wrong CHAP denied", False,
                       "positive CHAP login prerequisite failed")
        else:
            chap_error = _set_chap(iqn, ctx.host, chap_user,
                                   secrets.token_hex(7))
            wrong = (run([
                "iscsiadm", "-m", "node", "-T", iqn,
                "-p", f"{ctx.host}:3260", "--login",
            ], check=False) if chap_error is None else None)
            denied = wrong is not None and wrong.returncode != 0
            logged_in = wrong is not None and wrong.returncode == 0
            detail = chap_error or (
                "login unexpectedly succeeded" if not denied else "")
            if logged_in:
                logout = run([
                    "iscsiadm", "-m", "node", "-T", iqn,
                    "-p", f"{ctx.host}:3260", "--logout",
                ], check=False)
                if logout.returncode != 0:
                    raise RuntimeError(f"iSCSI logout: {logout.stderr.strip()}")
                logged_in = False

            restore_error = _set_chap(iqn, ctx.host, chap_user, chap_password)
            restored = (run([
                "iscsiadm", "-m", "node", "-T", iqn,
                "-p", f"{ctx.host}:3260", "--login",
            ], check=False) if restore_error is None else None)
            restored_ok = restored is not None and restored.returncode == 0
            logged_in = restored_ok
            if restored_ok:
                logout = run([
                    "iscsiadm", "-m", "node", "-T", iqn,
                    "-p", f"{ctx.host}:3260", "--logout",
                ], check=False)
                if logout.returncode != 0:
                    raise RuntimeError(f"iSCSI logout: {logout.stderr.strip()}")
                logged_in = False
            elif denied:
                detail = restore_error or "correct CHAP access was not restored"
            ctx.record("iSCSI auth: wrong CHAP denied",
                       denied and restored_ok, detail)
    except Exception as error:
        ctx.record("iSCSI auth: test", False, str(error))
    finally:
        cleanup_errors = []
        if logged_in and iqn:
            logout = run(["iscsiadm", "-m", "node", "-T", iqn,
                          "-p", f"{ctx.host}:3260", "--logout"], check=False)
            if logout.returncode == 0:
                logged_in = False
            else:
                cleanup_errors.append(f"logout: {logout.stderr.strip()}")
        if iqn:
            delete_node = run(["iscsiadm", "-m", "node", "-T", iqn,
                               "-p", f"{ctx.host}:3260", "-o", "delete"],
                              check=False)
            if delete_node.returncode != 0:
                cleanup_errors.append(f"node: {delete_node.stderr.strip()}")
        if target_id:
            try:
                await ctx.client.call("share.iscsi.delete", {"id": target_id})
            except Exception:
                pass
        cleanup_error = await _reconcile_share(ctx, "iscsi", name)
        if cleanup_error:
            ctx.record("iSCSI auth: share cleanup", False, cleanup_error)
        try:
            await ctx.client.call("subvolume.detach", {
                "filesystem": ctx.pool, "name": name,
            })
        except Exception:
            pass
        if cleanup_errors:
            ctx.record("iSCSI auth: client cleanup", False,
                       "; ".join(cleanup_errors))
        try:
            await ctx.client.call("subvolume.delete", {
                "filesystem": ctx.pool, "name": name,
            })
        except Exception:
            pass


async def test_nvmeof_auth(ctx: TestContext):
    header("NVMe-oF Host NQN Authorization")
    name = f"test-auth-nvme-{ctx.tag}"
    subsystem_id = None
    nqn = None
    connected = False

    try:
        with open("/etc/nvme/hostnqn") as hostnqn_file:
            host_nqn = hostnqn_file.read().strip()
        subvolume = await ctx.client.call("subvolume.create", {
            "filesystem": ctx.pool,
            "name": name,
            "subvolume_type": "block",
            "volsize_bytes": VOLUME_BYTES,
        })
        ctx.record("NVMe-oF auth: block volume created",
                   subvolume.get("created") is True)

        subsystem = await ctx.client.call("share.nvmeof.create", {
            "name": name,
            "device_path": subvolume["block_device"],
            "allow_any_host": False,
            "allowed_hosts": [host_nqn],
        })
        subsystem_id = subsystem["id"]
        nqn = subsystem["nqn"]
        restricted = (subsystem.get("allow_any_host") is False and
                      subsystem.get("allowed_hosts") == [host_nqn])
        ctx.record("NVMe-oF auth: restricted subsystem created",
                   restricted, str(subsystem))

        connect = run([
            "nvme", "connect", "-t", "tcp", "-n", nqn,
            "-a", ctx.host, "-s", "4420",
        ], check=False)
        connected = connect.returncode == 0
        ctx.record("NVMe-oF auth: allowed host connects", connected,
                    connect.stderr.strip())
        if not connected:
            ctx.record("NVMe-oF auth: revoked host denied", False,
                       "positive connection prerequisite failed")
        else:
            disconnect = run(["nvme", "disconnect", "-n", nqn], check=False)
            if disconnect.returncode != 0:
                ctx.record("NVMe-oF auth: revoked host denied", False,
                           f"positive disconnect failed: {disconnect.stderr.strip()}")
            else:
                connected = False
                await ctx.client.call("share.nvmeof.remove_host", {
                    "subsystem_id": subsystem_id,
                    "host_nqn": host_nqn,
                })
                denied = run([
                    "nvme", "connect", "-t", "tcp", "-n", nqn,
                    "-a", ctx.host, "-s", "4420",
                ], check=False)
                connected = denied.returncode == 0
                denied_ok = not connected
                detail = "connect unexpectedly succeeded" if connected else ""
                if connected:
                    disconnect = run(["nvme", "disconnect", "-n", nqn],
                                     check=False)
                    if disconnect.returncode != 0:
                        raise RuntimeError(
                            f"unauthorized disconnect: {disconnect.stderr.strip()}")
                    connected = False

                await ctx.client.call("share.nvmeof.add_host", {
                    "subsystem_id": subsystem_id,
                    "host_nqn": host_nqn,
                })
                restored = run([
                    "nvme", "connect", "-t", "tcp", "-n", nqn,
                    "-a", ctx.host, "-s", "4420",
                ], check=False)
                restored_ok = restored.returncode == 0
                connected = restored_ok
                if restored_ok:
                    disconnect = run(["nvme", "disconnect", "-n", nqn],
                                     check=False)
                    if disconnect.returncode != 0:
                        raise RuntimeError(
                            f"restored disconnect: {disconnect.stderr.strip()}")
                    connected = False
                elif denied_ok:
                    detail = "allowed host access was not restored"
                ctx.record("NVMe-oF auth: revoked host denied",
                           denied_ok and restored_ok, detail)
    except Exception as error:
        ctx.record("NVMe-oF auth: test", False, str(error))
    finally:
        cleanup_errors = []
        if connected and nqn:
            disconnect = run(["nvme", "disconnect", "-n", nqn], check=False)
            if disconnect.returncode != 0:
                cleanup_errors.append(f"disconnect: {disconnect.stderr.strip()}")
        if subsystem_id:
            try:
                await ctx.client.call("share.nvmeof.delete", {"id": subsystem_id})
            except Exception:
                pass
        cleanup_error = await _reconcile_share(ctx, "nvmeof", name)
        if cleanup_error:
            ctx.record("NVMe-oF auth: share cleanup", False, cleanup_error)
        try:
            await ctx.client.call("subvolume.detach", {
                "filesystem": ctx.pool, "name": name,
            })
        except Exception:
            pass
        if cleanup_errors:
            ctx.record("NVMe-oF auth: client cleanup", False,
                       "; ".join(cleanup_errors))
        try:
            await ctx.client.call("subvolume.delete", {
                "filesystem": ctx.pool, "name": name,
            })
        except Exception:
            pass
