from nasty.output import header, info, ok, warn

TEST_PREFIX = "test-"


async def delete_leftovers(client, pool_name: str):
    """Delete all test-* subvolumes and their shares left by --skip-delete runs."""
    header("Cleanup: Deleting test leftovers")

    subvolumes = await client.call("subvolume.list", {"pool": pool_name})
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
            for share in shares:
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
            warn(f"{proto} share cleanup: {e}")

    for sv in test_svs:
        info(f"Deleting subvolume '{sv['name']}'...")
        if sv.get("subvolume_type") == "block":
            try:
                await client.call("subvolume.detach", {"pool": pool_name, "name": sv["name"]})
            except Exception:
                pass
        try:
            await client.call("subvolume.delete", {"pool": pool_name, "name": sv["name"]})
            ok(f"Deleted '{sv['name']}'")
        except Exception as e:
            warn(f"Delete '{sv['name']}': {e}")
