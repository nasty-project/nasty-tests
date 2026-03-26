import os

from nasty.context import TestContext
from nasty.output import header, info
from nasty.shell import run


async def test_storage(ctx: TestContext):
    """Storage-level tests: compression and snapshot data integrity."""
    header("Storage Tests")

    await _test_compression(ctx)
    await _test_snapshot_integrity(ctx)


async def _test_compression(ctx: TestContext):
    info("── Compression ──────────────────────────────────────────")
    sv_name = f"test-storage-comp-{ctx.tag}"

    info(f"Creating subvolume '{sv_name}' with compression=zstd...")
    try:
        sv = await ctx.client.call("subvolume.create", {
            "pool": ctx.pool,
            "name": sv_name,
            "subvolume_type": "filesystem",
            "compression": "zstd",
        })
        ctx.record("compression: create with zstd", True)
    except Exception as e:
        ctx.record("compression: create with zstd", False, str(e))
        return

    try:
        got = await ctx.client.call("subvolume.get", {"pool": ctx.pool, "name": sv_name})
        comp = got.get("compression")
        ctx.record("compression: field returned correctly", comp == "zstd",
                   "" if comp == "zstd" else f"expected 'zstd', got {comp!r}")
    except Exception as e:
        ctx.record("compression: field returned correctly", False, str(e))

    if not ctx.skip_delete:
        try:
            await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": sv_name})
        except Exception:
            pass


async def _test_snapshot_integrity(ctx: TestContext):
    """
    Write data → snapshot → overwrite → clone snapshot.
    Verify: clone has pre-snapshot data, original has post-snapshot data.
    """
    info("── Snapshot data integrity ──────────────────────────────")

    sv_name    = f"test-storage-sv-{ctx.tag}"
    snap_name  = f"snap-integrity-{ctx.tag}"
    clone_name = f"test-storage-clone-{ctx.tag}"

    share_id       = None
    clone_share_id = None
    mp             = f"/tmp/nasty-storage-sv-{ctx.tag}"
    clone_mp       = f"/tmp/nasty-storage-clone-{ctx.tag}"
    mounted        = False
    clone_mounted  = False

    try:
        # Create subvolume + NFS share
        info(f"Creating subvolume '{sv_name}'...")
        try:
            sv = await ctx.client.call("subvolume.create", {
                "pool": ctx.pool,
                "name": sv_name,
                "subvolume_type": "filesystem",
            })
            ctx.record("snapshot integrity: subvolume created", True)
        except Exception as e:
            ctx.record("snapshot integrity: subvolume created", False, str(e))
            return

        try:
            share = await ctx.client.call("share.nfs.create", {
                "path": sv["path"],
                "clients": [{"host": "*", "options": "rw,sync,no_subtree_check,no_root_squash"}],
            })
            share_id = share["id"]
        except Exception as e:
            ctx.record("snapshot integrity: mount original", False, f"share create: {e}")
            return

        os.makedirs(mp, exist_ok=True)
        r = run(["mount", "-t", "nfs4", f"{ctx.host}:{sv['path']}", mp], check=False)
        if r.returncode != 0:
            ctx.record("snapshot integrity: mount original", False, r.stderr.strip())
            return
        mounted = True

        # Write pre-snapshot data
        before = f"before-snapshot-{ctx.tag}"
        with open(os.path.join(mp, "testfile.txt"), "w") as f:
            f.write(before)
        run(["sync"], check=False)

        # Take snapshot
        info(f"Taking snapshot '{snap_name}'...")
        try:
            await ctx.client.call("snapshot.create", {
                "pool": ctx.pool,
                "subvolume": sv_name,
                "name": snap_name,
                "read_only": True,
            })
            ctx.record("snapshot integrity: snapshot created", True)
        except Exception as e:
            ctx.record("snapshot integrity: snapshot created", False, str(e))
            return

        # Overwrite with post-snapshot data
        after = f"after-snapshot-{ctx.tag}"
        with open(os.path.join(mp, "testfile.txt"), "w") as f:
            f.write(after)
        run(["sync"], check=False)

        # Verify original has post-snapshot data
        with open(os.path.join(mp, "testfile.txt")) as f:
            got = f.read()
        ctx.record("snapshot integrity: original has post-snapshot data", got == after,
                   "" if got == after else f"expected '{after}', got '{got}'")

        # Clone snapshot
        info(f"Cloning snapshot → '{clone_name}'...")
        try:
            clone_sv = await ctx.client.call("snapshot.clone", {
                "pool": ctx.pool,
                "subvolume": sv_name,
                "snapshot": snap_name,
                "new_name": clone_name,
            })
            ctx.record("snapshot integrity: clone created", True)
        except Exception as e:
            ctx.record("snapshot integrity: clone created", False, str(e))
            return

        # Share + mount the clone
        try:
            clone_share = await ctx.client.call("share.nfs.create", {
                "path": clone_sv["path"],
                "clients": [{"host": "*", "options": "rw,sync,no_subtree_check,no_root_squash"}],
            })
            clone_share_id = clone_share["id"]
        except Exception as e:
            ctx.record("snapshot integrity: clone has pre-snapshot data", False, f"share create: {e}")
            return

        os.makedirs(clone_mp, exist_ok=True)
        r = run(["mount", "-t", "nfs4", f"{ctx.host}:{clone_sv['path']}", clone_mp], check=False)
        if r.returncode != 0:
            ctx.record("snapshot integrity: clone has pre-snapshot data", False, f"mount: {r.stderr.strip()}")
            return
        clone_mounted = True

        # Verify clone has pre-snapshot data
        with open(os.path.join(clone_mp, "testfile.txt")) as f:
            got = f.read()
        ctx.record("snapshot integrity: clone has pre-snapshot data", got == before,
                   "" if got == before else f"expected '{before}', got '{got}'")

    finally:
        if clone_mounted:
            run(["umount", clone_mp], check=False)
        if os.path.isdir(clone_mp):
            os.rmdir(clone_mp)
        if mounted:
            run(["umount", mp], check=False)
        if os.path.isdir(mp):
            os.rmdir(mp)
        if not ctx.skip_delete:
            if clone_share_id:
                try:
                    await ctx.client.call("share.nfs.delete", {"id": clone_share_id})
                except Exception:
                    pass
            try:
                await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": clone_name})
            except Exception:
                pass
            if share_id:
                try:
                    await ctx.client.call("share.nfs.delete", {"id": share_id})
                except Exception:
                    pass
            try:
                await ctx.client.call("snapshot.delete", {
                    "pool": ctx.pool, "subvolume": sv_name, "name": snap_name,
                })
            except Exception:
                pass
            try:
                await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": sv_name})
            except Exception:
                pass
