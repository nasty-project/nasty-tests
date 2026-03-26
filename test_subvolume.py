from nasty.context import TestContext
from nasty.output import header, info


async def test_subvolume(ctx: TestContext):
    """Subvolume lifecycle, properties (xattrs), and block attach/detach cycle."""
    header("Subvolume Tests")

    tag = ctx.tag

    # ── 1. Filesystem subvolume lifecycle ────────────────────────

    sv_name = f"test-sv-lifecycle-{tag}"
    info(f"Creating filesystem subvolume '{sv_name}'...")
    try:
        sv = await ctx.client.call("subvolume.create", {
            "pool": ctx.pool,
            "name": sv_name,
            "subvolume_type": "filesystem",
            "comments": "lifecycle test",
        })
        ctx.record("subvolume lifecycle: create", True)
    except Exception as e:
        ctx.record("subvolume lifecycle: create", False, str(e))
        return

    info("Verifying subvolume appears in list...")
    svs = await ctx.client.call("subvolume.list", {"pool": ctx.pool})
    found = any(s["name"] == sv_name for s in svs)
    ctx.record("subvolume lifecycle: appears in list", found,
               "" if found else f"'{sv_name}' not in list")

    info(f"Getting subvolume '{sv_name}'...")
    try:
        got = await ctx.client.call("subvolume.get", {"pool": ctx.pool, "name": sv_name})
        ctx.record("subvolume lifecycle: get", got["name"] == sv_name,
                   "" if got["name"] == sv_name else f"name mismatch: {got['name']}")
    except Exception as e:
        ctx.record("subvolume lifecycle: get", False, str(e))

    if not ctx.skip_delete:
        info(f"Deleting subvolume '{sv_name}'...")
        try:
            await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": sv_name})
            ctx.record("subvolume lifecycle: delete", True)
        except Exception as e:
            ctx.record("subvolume lifecycle: delete", False, str(e))
            return

        info("Verifying subvolume is gone from list...")
        svs = await ctx.client.call("subvolume.list", {"pool": ctx.pool})
        gone = not any(s["name"] == sv_name for s in svs)
        ctx.record("subvolume lifecycle: absent after delete", gone,
                   "" if gone else f"'{sv_name}' still in list after delete")

    # ── 2. Properties (xattrs) ───────────────────────────────────

    prop_sv = f"test-sv-props-{tag}"
    info(f"Creating subvolume '{prop_sv}' for property tests...")
    try:
        await ctx.client.call("subvolume.create", {
            "pool": ctx.pool,
            "name": prop_sv,
            "subvolume_type": "filesystem",
        })
    except Exception as e:
        ctx.record("subvolume properties: create", False, str(e))
        return
    ctx.record("subvolume properties: create", True)

    props = {"nasty-csi/pvc": f"pvc-{tag}", "nasty-csi/ns": "default"}
    info("Setting properties...")
    try:
        updated = await ctx.client.call("subvolume.set_properties", {
            "pool": ctx.pool,
            "name": prop_sv,
            "properties": props,
        })
        actual = updated.get("properties", {})
        ok_set = all(actual.get(k) == v for k, v in props.items())
        ctx.record("subvolume properties: set", ok_set,
                   "" if ok_set else f"mismatch: {actual}")
    except Exception as e:
        ctx.record("subvolume properties: set", False, str(e))

    info("Finding subvolume by property...")
    try:
        results = await ctx.client.call("subvolume.find_by_property", {
            "pool": ctx.pool,
            "key": "nasty-csi/pvc",
            "value": f"pvc-{tag}",
        })
        found_prop = any(s["name"] == prop_sv for s in results)
        ctx.record("subvolume properties: find_by_property", found_prop,
                   "" if found_prop else f"'{prop_sv}' not found by property")
    except Exception as e:
        ctx.record("subvolume properties: find_by_property", False, str(e))

    info("Removing one property key...")
    try:
        updated = await ctx.client.call("subvolume.remove_properties", {
            "pool": ctx.pool,
            "name": prop_sv,
            "keys": ["nasty-csi/pvc"],
        })
        actual = updated.get("properties", {})
        removed = "nasty-csi/pvc" not in actual
        ctx.record("subvolume properties: remove", removed,
                   "" if removed else f"key still present: {actual}")
    except Exception as e:
        ctx.record("subvolume properties: remove", False, str(e))

    if not ctx.skip_delete:
        try:
            await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": prop_sv})
        except Exception:
            pass

    # ── 3. Block subvolume attach/detach cycle ───────────────────

    block_sv = f"test-sv-block-{tag}"
    info(f"Creating block subvolume '{block_sv}' (32 MiB)...")
    try:
        sv = await ctx.client.call("subvolume.create", {
            "pool": ctx.pool,
            "name": block_sv,
            "subvolume_type": "block",
            "volsize_bytes": 32 * 1024 * 1024,
        })
        has_dev = bool(sv.get("block_device"))
        ctx.record("block attach/detach: create with loop device", has_dev,
                   "" if has_dev else "no block_device returned after create")
    except Exception as e:
        ctx.record("block attach/detach: create with loop device", False, str(e))
        return

    info("Detaching loop device...")
    try:
        detached = await ctx.client.call("subvolume.detach", {
            "pool": ctx.pool,
            "name": block_sv,
        })
        no_dev = detached.get("block_device") is None
        ctx.record("block attach/detach: detach", no_dev,
                   "" if no_dev else f"block_device still set: {detached.get('block_device')}")
    except Exception as e:
        ctx.record("block attach/detach: detach", False, str(e))

    info("Re-attaching loop device...")
    try:
        attached = await ctx.client.call("subvolume.attach", {
            "pool": ctx.pool,
            "name": block_sv,
        })
        has_dev = bool(attached.get("block_device"))
        ctx.record("block attach/detach: re-attach", has_dev,
                   "" if has_dev else "no block_device after re-attach")
    except Exception as e:
        ctx.record("block attach/detach: re-attach", False, str(e))

    # ── 4. Block subvolume resize ─────────────────────────────────

    resize_sv = f"test-sv-resize-{tag}"
    initial_size = 32 * 1024 * 1024   # 32 MiB
    new_size     = 64 * 1024 * 1024   # 64 MiB
    info(f"Creating block subvolume '{resize_sv}' ({initial_size // (1024*1024)} MiB) for resize test...")
    try:
        sv = await ctx.client.call("subvolume.create", {
            "pool": ctx.pool,
            "name": resize_sv,
            "subvolume_type": "block",
            "volsize_bytes": initial_size,
        })
        ctx.record("block resize: create", sv.get("volsize_bytes") == initial_size,
                   "" if sv.get("volsize_bytes") == initial_size
                   else f"expected {initial_size}, got {sv.get('volsize_bytes')}")
    except Exception as e:
        ctx.record("block resize: create", False, str(e))
        goto_cleanup = True
    else:
        goto_cleanup = False

    if not goto_cleanup:
        info(f"Resizing '{resize_sv}' to {new_size // (1024*1024)} MiB...")
        try:
            resized = await ctx.client.call("subvolume.resize", {
                "pool": ctx.pool,
                "name": resize_sv,
                "volsize_bytes": new_size,
            })
            ctx.record("block resize: volsize_bytes updated",
                       resized.get("volsize_bytes") == new_size,
                       "" if resized.get("volsize_bytes") == new_size
                       else f"expected {new_size}, got {resized.get('volsize_bytes')}")
        except Exception as e:
            ctx.record("block resize: volsize_bytes updated", False, str(e))

    if not ctx.skip_delete:
        try:
            await ctx.client.call("subvolume.detach", {"pool": ctx.pool, "name": resize_sv})
        except Exception:
            pass
        try:
            await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": resize_sv})
        except Exception:
            pass

    if not ctx.skip_delete:
        try:
            await ctx.client.call("subvolume.detach", {"pool": ctx.pool, "name": block_sv})
        except Exception:
            pass
        try:
            await ctx.client.call("subvolume.delete", {"pool": ctx.pool, "name": block_sv})
        except Exception:
            pass
