from nasty.output import header, info, ok, warn

TEST_PREFIX = "test-"


async def delete_leftovers(client, pool_name: str):
    """Delete all test-* subvolumes and their shares left by --skip-delete runs."""
    header("Cleanup: Deleting test leftovers")
    errors = []

    subvolumes = await client.call("subvolume.list", {"filesystem": pool_name})
    test_svs = [sv for sv in subvolumes if sv["name"].startswith(TEST_PREFIX)]
    test_paths = {sv["path"] for sv in test_svs}

    def _share_name(proto, share):
        """Return the human-readable name/identifier for a share object."""
        if proto == "iSCSI":
            return share.get("iqn", "").rsplit(":", 1)[-1]
        if proto == "NVMe-oF":
            return share.get("nqn", "").rsplit(":", 1)[-1]
        return share.get("name", "")

    for proto, list_method, delete_method, path_key in [
        ("NFS",     "share.nfs.list",    "share.nfs.delete",    "path"),
        ("SMB",     "share.smb.list",    "share.smb.delete",    "path"),
        ("iSCSI",   "share.iscsi.list",  "share.iscsi.delete",  None),
        ("NVMe-oF", "share.nvmeof.list", "share.nvmeof.delete", None),
    ]:
        try:
            shares = await client.call(list_method)
        except Exception as e:
            errors.append(f"{proto} share list: {e}")
            continue
        for share in shares:
            try:
                match = (
                    share.get("path") in test_paths if path_key
                    else _share_name(proto, share).startswith(TEST_PREFIX)
                )
                if match:
                    label = _share_name(proto, share) or share["id"]
                    info(f"Deleting {proto} share '{label}'...")
                    await client.call(delete_method, {"id": share["id"]})
                    ok("Deleted")
            except Exception as e:
                message = f"{proto} share cleanup: {e}"
                errors.append(message)
                warn(message)

    try:
        snapshots = await client.call("snapshot.list", {"filesystem": pool_name})
    except Exception as e:
        snapshots = []
        errors.append(f"snapshot list: {e}")
    test_names = {sv["name"] for sv in test_svs}
    for snapshot in snapshots:
        if snapshot.get("subvolume") not in test_names:
            continue
        label = f"{snapshot['subvolume']}/{snapshot['name']}"
        info(f"Deleting snapshot '{label}'...")
        try:
            await client.call("snapshot.delete", {
                "filesystem": pool_name,
                "subvolume": snapshot["subvolume"],
                "name": snapshot["name"],
            })
            ok("Deleted")
        except Exception as e:
            message = f"Delete snapshot '{label}': {e}"
            errors.append(message)
            warn(message)

    for sv in test_svs:
        info(f"Deleting subvolume '{sv['name']}'...")
        if sv.get("subvolume_type") == "block":
            try:
                await client.call("subvolume.detach", {"filesystem": pool_name, "name": sv["name"]})
            except Exception:
                pass
        try:
            await client.call("subvolume.delete", {"filesystem": pool_name, "name": sv["name"]})
            ok(f"Deleted '{sv['name']}'")
        except Exception as e:
            message = f"Delete '{sv['name']}': {e}"
            errors.append(message)
            warn(message)

    remaining = await client.call("subvolume.list", {"filesystem": pool_name})
    residual_names = [sv["name"] for sv in remaining if sv["name"].startswith(TEST_PREFIX)]
    if residual_names:
        errors.append(f"residual subvolumes: {', '.join(sorted(residual_names))}")
    return errors
