import asyncio
import json
import os
import ssl
import time
import urllib.request
from dataclasses import dataclass

from nasty.context import TestContext
from nasty.output import header, info
from nasty.shell import cleanup_mount, run, validate_format_device
from test_iscsi import find_iscsi_device
from test_nvmeof import find_nvme_device


VOLUME_BYTES = 64 * 1024 * 1024


@dataclass
class ExportState:
    protocol: str
    subvolume: str
    block_device: str
    mountpoint: str
    marker: str
    share_id: str | None = None
    qualified_name: str | None = None
    block_volume_id: dict | None = None
    fs_uuid: str | None = None
    connected: bool = False
    mounted: bool = False
    identity_verified: bool = False
    restored_identity_verified: bool = False


def _backing(export: dict, protocol: str) -> tuple[dict | None, str | None, bool]:
    collection = export.get("luns", []) if protocol == "iscsi" else export.get("namespaces", [])
    if not collection:
        return None, None, True
    item = collection[0]
    path_key = "backstore_path" if protocol == "iscsi" else "device_path"
    return (
        item.get("backing_volume"),
        item.get(path_key),
        item.get("backing_volume_unresolved", False),
    )


async def _wait_for_device(state: ExportState, timeout: int = 30) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        device = (find_iscsi_device(state.qualified_name)
                  if state.protocol == "iscsi"
                  else find_nvme_device(state.qualified_name))
        if device:
            return device
        await asyncio.sleep(1)
    return None


async def _connect(ctx: TestContext, state: ExportState) -> tuple[bool, str]:
    if state.protocol == "iscsi" and state.qualified_name:
        discovery = run([
            "iscsiadm", "-m", "discovery", "-t", "sendtargets", "-p", ctx.host,
        ], check=False)
        if discovery.returncode != 0 or state.qualified_name not in discovery.stdout:
            return False, discovery.stderr.strip() or "IQN not discovered"
        result = run([
            "iscsiadm", "-m", "node", "-T", state.qualified_name,
            "-p", f"{ctx.host}:3260", "--login",
        ], check=False)
    else:
        result = run([
            "nvme", "connect", "-t", "tcp", "-n", state.qualified_name,
            "-a", ctx.host, "-s", "4420",
        ], check=False)
    state.connected = result.returncode == 0
    return state.connected, result.stderr.strip()


def _prepare_mountpoint(path: str) -> str | None:
    if not path.startswith("/tmp/nasty-test-"):
        return f"refusing unsafe mountpoint: {path}"
    if os.path.lexists(path):
        return f"refusing pre-existing mountpoint: {path}"
    os.mkdir(path, mode=0o700)
    return None


def _disconnect(ctx: TestContext, state: ExportState):
    error = cleanup_mount(state.mountpoint, state.mounted)
    if error:
        return error
    state.mounted = False
    if state.protocol == "iscsi" and state.qualified_name:
        if state.connected:
            logout = run([
                "iscsiadm", "-m", "node", "-T", state.qualified_name,
                "-p", f"{ctx.host}:3260", "--logout",
            ], check=False)
            if logout.returncode != 0:
                return f"iSCSI logout: {logout.stderr.strip()}"
        run([
            "iscsiadm", "-m", "node", "-T", state.qualified_name,
            "-p", f"{ctx.host}:3260", "-o", "delete",
        ], check=False)
    elif state.protocol == "nvmeof" and state.connected and state.qualified_name:
        disconnect = run(["nvme", "disconnect", "-n", state.qualified_name], check=False)
        if disconnect.returncode != 0:
            return f"NVMe disconnect: {disconnect.stderr.strip()}"
    state.connected = False
    return error


async def _prepare_data(ctx: TestContext, state: ExportState):
    connected, detail = await _connect(ctx, state)
    ctx.record(f"{state.protocol} persistence: initial connect", connected, detail)
    if not connected:
        return

    device = await _wait_for_device(state)
    pattern = r"/dev/sd[a-z]+" if state.protocol == "iscsi" else r"/dev/nvme[0-9]+n[0-9]+"
    safe_error = ("device not found" if not device else
                  validate_format_device(device, pattern, VOLUME_BYTES))
    ctx.record(f"{state.protocol} persistence: initial device safe",
               safe_error is None, safe_error or "")
    if safe_error:
        return

    formatted = run(["mkfs.ext4", "-F", "-q", device], check=False, timeout=60)
    if formatted.returncode == 0:
        mountpoint_error = _prepare_mountpoint(state.mountpoint)
        mounted = (run(["mount", device, state.mountpoint], check=False)
                   if mountpoint_error is None else None)
    else:
        mounted = formatted
        mountpoint_error = None
    state.mounted = (formatted.returncode == 0 and mounted is not None and
                     mounted.returncode == 0)
    detail = mountpoint_error or mounted.stderr.strip()
    if state.mounted:
        with open(os.path.join(state.mountpoint, "identity-marker.txt"), "w") as marker_file:
            marker_file.write(state.marker)
            marker_file.flush()
            os.fsync(marker_file.fileno())
        run(["sync", "-f", state.mountpoint], check=True)
    ctx.record(f"{state.protocol} persistence: initial data written", state.mounted, detail)

    uuid_result = run(["blkid", "-s", "UUID", "-o", "value", device], check=False)
    state.fs_uuid = uuid_result.stdout.strip() if uuid_result.returncode == 0 else None
    ctx.record(f"{state.protocol} persistence: filesystem UUID captured",
               bool(state.fs_uuid), uuid_result.stderr.strip())


async def _verify_data(ctx: TestContext, state: ExportState):
    connected, detail = await _connect(ctx, state)
    ctx.record(f"{state.protocol} persistence: reconnect", connected, detail)
    if not connected:
        ctx.record(f"{state.protocol} persistence: filesystem UUID preserved",
                   False, "reconnect failed")
        ctx.record(f"{state.protocol} persistence: marker data preserved",
                   False, "reconnect failed")
        return

    device = await _wait_for_device(state)
    if not device:
        ctx.record(f"{state.protocol} persistence: filesystem UUID preserved",
                   False, "device not found")
        ctx.record(f"{state.protocol} persistence: marker data preserved",
                   False, "device not found")
        return

    uuid_result = run(["blkid", "-s", "UUID", "-o", "value", device], check=False)
    actual_uuid = uuid_result.stdout.strip()
    uuid_matches = actual_uuid == state.fs_uuid
    ctx.record(f"{state.protocol} persistence: filesystem UUID preserved",
               uuid_matches,
               "" if uuid_matches else f"expected {state.fs_uuid}, got {actual_uuid}")
    if not uuid_matches:
        ctx.record(f"{state.protocol} persistence: marker data preserved", False,
                   "filesystem UUID verification failed")
        return

    mountpoint_error = _prepare_mountpoint(state.mountpoint)
    mounted = (run(["mount", device, state.mountpoint], check=False)
               if mountpoint_error is None else None)
    state.mounted = mounted is not None and mounted.returncode == 0
    try:
        if not state.mounted:
            raise RuntimeError(mountpoint_error or mounted.stderr.strip())
        with open(os.path.join(state.mountpoint, "identity-marker.txt")) as marker_file:
            actual_marker = marker_file.read()
        marker_ok = actual_marker == state.marker
        detail = "" if marker_ok else f"expected {state.marker}, got {actual_marker}"
    except Exception as e:
        marker_ok = False
        detail = str(e)
    ctx.record(f"{state.protocol} persistence: marker data preserved", marker_ok, detail)


async def _wait_for_restarted_api(ctx: TestContext, started_before: int,
                                  timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            await ctx.client.reconnect()
            started_after = await asyncio.to_thread(_process_started_at, ctx)
            if started_after > started_before:
                return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


def _process_started_at(ctx: TestContext) -> int:
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    response = urllib.request.urlopen(
        f"https://{ctx.host}:{ctx.client.port}/api/boot_status",
        context=ssl_ctx,
        timeout=10,
    )
    return json.loads(response.read())["process_started_at_unix"]


async def test_block_persistence(ctx: TestContext, restart_timeout: int = 180):
    header("Block Export Persistence (engine restart)")
    states = []

    try:
        for protocol in ("iscsi", "nvmeof"):
            name = f"test-persist-{protocol}-{ctx.tag}"
            existing_subvolumes = await ctx.client.call(
                "subvolume.list", {"filesystem": ctx.pool})
            if any(sv.get("name") == name for sv in existing_subvolumes):
                raise RuntimeError(f"refusing existing subvolume: {name}")
            existing_exports = await ctx.client.call(f"share.{protocol}.list")
            identity_key = "iqn" if protocol == "iscsi" else "nqn"
            if any(export.get(identity_key, "").endswith(f":{name}")
                   for export in existing_exports):
                raise RuntimeError(f"refusing existing {protocol} export: {name}")

            info(f"Creating {protocol} persistence volume '{name}'...")
            subvolume = await ctx.client.call("subvolume.create", {
                "filesystem": ctx.pool,
                "name": name,
                "subvolume_type": "block",
                "volsize_bytes": VOLUME_BYTES,
            })
            if subvolume.get("created") is not True:
                raise RuntimeError(f"subvolume was not freshly created: {name}")
            block_volume_id = subvolume.get("block_volume_id")
            ctx.record(f"{protocol} persistence: immutable block identity",
                       bool(block_volume_id), "block_volume_id missing")

            state = ExportState(
                protocol=protocol,
                subvolume=name,
                block_device=subvolume["block_device"],
                mountpoint=f"/tmp/nasty-test-persist-{protocol}-{ctx.tag}",
                marker=f"nasty-{protocol}-persistence-{ctx.tag}",
                block_volume_id=block_volume_id,
            )
            states.append(state)

            method = f"share.{protocol}.create"
            export = await ctx.client.call(method, {
                "name": name,
                "device_path": subvolume["block_device"],
            })
            state.share_id = export["id"]
            export = await ctx.client.call(f"share.{protocol}.get", {"id": export["id"]})
            backing, path, unresolved = _backing(export, protocol)
            identity_ok = (bool(block_volume_id) and backing == block_volume_id and
                           path == subvolume["block_device"] and not unresolved)
            ctx.record(f"{protocol} persistence: export identity captured",
                       identity_ok, f"backing={backing}, path={path}, unresolved={unresolved}")
            state.identity_verified = identity_ok

            qualified_name = export["iqn"] if protocol == "iscsi" else export["nqn"]
            state.qualified_name = qualified_name

        if any(not state.identity_verified for state in states):
            return

        for state in states:
            await _prepare_data(ctx, state)
        if any(not state.mounted or not state.fs_uuid for state in states):
            return

        for state in states:
            error = _disconnect(ctx, state)
            if error:
                raise RuntimeError(f"{state.protocol} pre-restart cleanup: {error}")

        started_before = await asyncio.to_thread(_process_started_at, ctx)
        info("Scheduling a detached nasty-engine restart...")
        try:
            await ctx.client.run_terminal([
                "systemd-run",
                f"--unit=nasty-test-restart-{ctx.tag}",
                "--on-active=1s",
                "--collect",
                "systemctl", "restart", "nasty-engine.service",
            ])
            restart_scheduled = True
            restart_detail = ""
        except Exception as e:
            restart_scheduled = False
            restart_detail = str(e)
        ctx.record("block persistence: engine restart", restart_scheduled, restart_detail)
        if not restart_scheduled:
            return

        await asyncio.sleep(2)
        reconnected = await _wait_for_restarted_api(
            ctx, started_before, restart_timeout)
        ctx.record("block persistence: API reconnected", reconnected,
                   "engine process timestamp did not advance before timeout")
        if not reconnected:
            return

        for state in states:
            subvolume = await ctx.client.call("subvolume.get", {
                "filesystem": ctx.pool, "name": state.subvolume,
            })
            export = await ctx.client.call(
                f"share.{state.protocol}.get", {"id": state.share_id})
            qualified_name = export["iqn"] if state.protocol == "iscsi" else export["nqn"]
            ctx.record(f"{state.protocol} persistence: export identity stable",
                       qualified_name == state.qualified_name,
                       f"expected {state.qualified_name}, got {qualified_name}")

            backing, path, unresolved = _backing(export, state.protocol)
            current_identity = subvolume.get("block_volume_id")
            backing_stable = (bool(state.block_volume_id) and
                              current_identity == state.block_volume_id and
                              backing == state.block_volume_id)
            ctx.record(f"{state.protocol} persistence: immutable backing stable",
                       backing_stable,
                       f"expected {state.block_volume_id}, subvolume={current_identity}, backing={backing}")
            resolved = path == subvolume.get("block_device") and not unresolved
            ctx.record(f"{state.protocol} persistence: backing path resolved",
                       resolved,
                       f"path={path}, current={subvolume.get('block_device')}, unresolved={unresolved}")
            state.restored_identity_verified = (
                qualified_name == state.qualified_name and backing_stable and resolved)

        for state in states:
            if state.restored_identity_verified:
                await _verify_data(ctx, state)
            else:
                ctx.record(f"{state.protocol} persistence: reconnect", False,
                           "restored export identity verification failed")
                ctx.record(f"{state.protocol} persistence: filesystem UUID preserved", False,
                           "restored export identity verification failed")
                ctx.record(f"{state.protocol} persistence: marker data preserved", False,
                           "restored export identity verification failed")
    except Exception as e:
        ctx.record("block persistence: test", False, str(e))
    finally:
        for state in states:
            error = _disconnect(ctx, state)
            if error:
                ctx.record(f"{state.protocol} persistence: client cleanup", False, error)
        if not ctx.skip_delete:
            for state in reversed(states):
                if state.mounted or state.connected:
                    continue
                if not state.share_id:
                    try:
                        shares = await ctx.client.call(f"share.{state.protocol}.list")
                        expected_key = "iqn" if state.protocol == "iscsi" else "nqn"
                        partial = next((share for share in shares
                                        if share.get(expected_key, "").endswith(
                                            f":{state.subvolume}")), None)
                        if partial:
                            state.share_id = partial["id"]
                    except Exception:
                        pass
                if state.share_id:
                    try:
                        await ctx.client.call(
                            f"share.{state.protocol}.delete", {"id": state.share_id})
                    except Exception:
                        pass
                try:
                    await ctx.client.call("subvolume.detach", {
                        "filesystem": ctx.pool, "name": state.subvolume,
                    })
                except Exception:
                    pass
                try:
                    await ctx.client.call("subvolume.delete", {
                        "filesystem": ctx.pool, "name": state.subvolume,
                    })
                except Exception:
                    pass
