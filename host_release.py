#!/usr/bin/env python3
"""host_release.py - release ESXi hosts from a source vCenter for rebuild elsewhere.

Takes a host out of a live cluster and leaves it standalone, which is the state
esx_ready requires: esx_ready skips a host whose vmk0 is on a DVS or that reports
a managing vCenter.

  python3 host_release.py survey  --config env.yaml --site <site>
  python3 host_release.py plan    --config env.yaml --site <site>
  python3 host_release.py apply   --config env.yaml --site <site> [--commit]
  python3 host_release.py verify  --config env.yaml --site <site>

Phases, each idempotent and resumable, state read from live inventory:

  check      the host exists, is where the config says, and the cluster can lose it
  evacuate   no powered-on VMs left on the host
  drain      maintenance mode with full data migration   <- the multi-hour one
  disks      vSAN disk groups removed
  uncluster  out of the cluster to the datacenter level, which is what makes
             NSX uninstall from a host it manages through the cluster
  nsx        transport node gone from the source NSX, force-deleted if it lingers
  release    disconnected and out of inventory: standalone, for esx_ready

Writes only with --commit. One host drains at a time by default (--parallel).
"""

import argparse
import os
import ssl
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deploy_vc as dv
from deploy_vc import OK, ADD, CHANGE, BLOCKED, SKIP, Result, die, opt

VERSION = "1.0"

# The source block, validated here because this is the tool that owns it.
# deploy_vc carries "source": "*" so the same env file passes both.
KNOWN_SOURCE = {
    "vcenter": None, "user": None, "password": None,
    "cluster": None,
    "nsx": None, "nsx_user": None, "nsx_password": None,
    "hosts": None,
    "min_free_pct": None,
}

# vSAN wants headroom for resync and slack. Refuse an evacuation that would
# leave the cluster below this, because the operator cannot undo it.
DEFAULT_MIN_FREE_PCT = 25


# ------------------------------------------------------------------ config

def unanswered(v):
    """[set] and [verify] parse as a one-item list, so they are truthy and would
    otherwise reach a connection attempt as a hostname."""
    if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
        return v[0].strip().lower() in ("set", "verify")
    if isinstance(v, str):
        return v.strip().lower() in ("[set]", "[verify]", "set", "verify")
    return False


def load_config(path):
    """Parse the env file without deploy_vc's schema check.

    That check rejects any key deploy_vc does not know, which would make this
    tool fail on a config deploy_vc has not been taught about yet. Only the
    source: block is this tool's to validate, and source_config does that.
    """
    with open(path) as f:
        text = f.read()
    try:
        import yaml
        cfg = yaml.safe_load(text)
    except ImportError:
        cfg = dv.parse_yaml(text)
    if not isinstance(cfg, dict):
        die("%s did not parse as a mapping" % path)
    cfg["__path"] = path
    return cfg


def source_config(cfg, site):
    """The source: block for one site, validated."""
    sites = cfg.get("sites") or {}
    if not sites:
        die("%s has no sites: block, so it is not a full env file. A fragment meant "
            "to be inserted into one will look like this." % cfg.get("__path", "the config"))
    if site not in sites:
        die("no site %r in the config. Known: %s" % (site, ", ".join(sorted(sites))))
    src = (sites[site] or {}).get("source")
    if not src:
        die("site %r has no source: block. It names the source vCenter and the "
            "hosts:\n\n%s" % (site, EXAMPLE.strip()))
    bad = dv.check_keys(src, KNOWN_SOURCE, "sites.%s.source" % site)
    for name, near in bad:
        die("%s is not a name this tool knows%s" % (name, " - did you mean %s?" % near if near else ""))
    if not src.get("hosts"):
        die("sites.%s.source.hosts is required" % site)
    if not src.get("vcenter"):
        where = "sites.%s.source.vcenter" % site
        if not sys.stdin.isatty():
            die("%s is blank and there is no terminal to ask on" % where)
        got = input("      vCenter these hosts are in today (%s): " % where).strip()
        if not got:
            die("%s is required" % where)
        src = dict(src)
        src["vcenter"] = got
    still = [k for k, v in src.items() if unanswered(v)]
    if still:
        die("sites.%s.source is unanswered: %s\n"
            "Marked [set] or [verify] in the config; fill them in."
            % (site, ", ".join(sorted(still))))
    hosts = src["hosts"]
    if isinstance(hosts, str):
        hosts = [hosts]
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts):
        die("sites.%s.source.hosts must be a list of host names" % site)
    src = dict(src)
    src["hosts"] = [h.strip() for h in hosts]
    dupes = {h for h in src["hosts"] if src["hosts"].count(h) > 1}
    if dupes:
        die("sites.%s.source.hosts lists %s twice" % (site, ", ".join(sorted(dupes))))
    return src


EXAMPLE = """
sites:
  my-site:
    # ... the deploy_vc keys that build the destination ...

    # host_release only. deploy_vc ignores this block.
    source:
      vcenter: oldvc.example.com        # the vCenter these hosts come OUT of
      user: administrator@vsphere.local
      password:                         # blank: asked for by name, never stored
      cluster: old-cluster-01           # optional: refuse a host found elsewhere
      nsx: oldnsx.example.com           # optional: only if these hosts are NSX-prepared
      nsx_user: admin
      nsx_password:
      min_free_pct: 25                  # refuse an evacuation leaving vSAN tighter
      hosts:                            # exactly the hosts to release. No wildcards.
        - esx13.example.com
        - esx14.example.com
"""


# ------------------------------------------------------------------ state

def host_obj(vs, fqdn):
    """The HostSystem, matched on name or on its primary address."""
    short = fqdn.split(".")[0].lower()
    for h in vs._all(vs.vim.HostSystem):
        name = (h.name or "").lower()
        if name == fqdn.lower() or name.split(".")[0] == short:
            return h
    return None


def powered_on(host):
    return [vm for vm in getattr(host, "vm", []) or []
            if getattr(vm.runtime, "powerState", "") == "poweredOn"]


def _bytes(disk):
    cap = getattr(disk, "capacity", None)
    if cap is None:
        return 0
    return int(getattr(cap, "block", 0)) * int(getattr(cap, "blockSize", 0))


def vsan_disks(host):
    """(claimed disks, capacity-tier bytes) for this host.

    Only the capacity tier counts. A vSAN datastore's size is the capacity
    drives alone, so including the cache disk here would compare a host total
    against a cluster figure that never had cache in it. On all-flash the
    capacity drives are the mapping's nonSsd list, the cache disk is ssd.
    """
    try:
        disks = host.configManager.vsanSystem.QueryDisksForVsan() or []
    except Exception:
        return [], 0
    claimed = [d for d in disks if getattr(d, "state", "") == "inUse"]
    try:
        mappings = list(host.config.vsanHostConfig.storageInfo.diskMapping or [])
    except Exception:
        mappings = []
    if mappings:
        return claimed, sum(_bytes(d) for m in mappings for d in (getattr(m, "nonSsd", None) or []))
    # No readable mapping: fall back to every claimed disk, which overstates by
    # the cache tier and so only ever refuses an evacuation it might have passed.
    return claimed, sum(_bytes(getattr(d, "disk", None)) for d in claimed)


def running_task(vs, host, kinds=("EnterMaintenanceMode_Task", "EnterMaintenanceMode")):
    """A task of ours still running against this host, if any."""
    try:
        tasks = vs.content.taskManager.recentTask or []
    except Exception:
        return None
    for t in tasks:
        info = getattr(t, "info", None)
        if not info or info.state not in ("queued", "running"):
            continue
        if getattr(info, "entity", None) == host and (info.name or "") in kinds:
            return info
    return None


def nsx_residue(host):
    """NSX state still on the host: the N-VDS shows as an opaque switch and TEPs
    as vmkernels on the vxlan stack. Both survive a manager-side delete."""
    found = []
    try:
        net = host.config.network
    except Exception:
        return found
    for sw in getattr(net, "opaqueSwitch", []) or []:
        kind = (getattr(sw, "type", "") or "").lower()
        if "nsx" in kind or kind == "":
            found.append("opaque switch %s" % (getattr(sw, "name", None) or kind or "?"))
    for vnic in getattr(net, "vnic", []) or []:
        stack = getattr(getattr(vnic, "spec", None), "netStackInstanceKey", "") or ""
        if stack.lower() == "vxlan":
            found.append("%s on the vxlan stack" % vnic.device)
    return found


def short_set(names):
    return {str(x).split(".")[0].lower() for x in names}


def whole_cluster_leaving(cl, releasing):
    """True when every host in the cluster is on the release list.

    Then there is no cluster left to evacuate to, so the capacity check means
    nothing: what matters is whatever data is abandoned with it.
    """
    members = short_set(getattr(h, "name", "") for h in (getattr(cl, "host", []) or []))
    return bool(members) and members <= short_set(releasing)


def vsan_used(cl):
    """Bytes written to the cluster's vSAN datastore, or None if unreadable."""
    for d in getattr(cl, "datastore", []) or []:
        sm = getattr(d, "summary", None)
        if getattr(sm, "type", "") == "vsan":
            cap = int(getattr(sm, "capacity", 0) or 0)
            free = int(getattr(sm, "freeSpace", 0) or 0)
            return (cap - free) if cap > 0 else None
    return None


def cluster_of(host):
    parent = getattr(host, "parent", None)
    kind = type(parent).__name__ if parent is not None else ""
    return parent if "ClusterComputeResource" in kind else None


def host_state(vs, fqdn, nsx_nodes):
    """Everything the phases need, read once.

    Every key is always present, including for a host that has already left the
    vCenter. A phase that reads one it did not expect to need is a KeyError in
    the middle of a committed run, which is a worse failure than a dull default.
    """
    h = host_obj(vs, fqdn)
    short = fqdn.split(".")[0].lower()
    tn = None
    for node in nsx_nodes:
        nm = (node.get("display_name") or "").lower()
        if nm == fqdn.lower() or nm.split(".")[0] == short:
            tn = node
            break
    if h is None:
        # Gone from the vCenter. NSX is still worth reporting: a transport node
        # left behind here is the stale record a force delete clears.
        return {
            "fqdn": fqdn, "present": False, "host": None,
            "cluster": None, "cluster_name": None, "connection": None,
            "maintenance": False, "vms_on": [], "vms_all": [],
            "vsan_disks": [], "vsan_bytes": 0, "task": None,
            "tn": tn, "nsx_left": [],
        }
    cl = cluster_of(h)
    claimed, cap = vsan_disks(h)
    return {
        "fqdn": fqdn, "present": True, "host": h,
        "cluster": cl, "cluster_name": cl.name if cl is not None else None,
        "connection": getattr(h.runtime, "connectionState", "?"),
        "maintenance": bool(getattr(h.runtime, "inMaintenanceMode", False)),
        "vms_on": powered_on(h),
        "vms_all": list(getattr(h, "vm", []) or []),
        "vsan_disks": claimed, "vsan_bytes": cap,
        "task": running_task(vs, h),
        "tn": tn, "nsx_left": nsx_residue(h),
    }


def gb(n):
    return "%.1f TB" % (n / 1e12) if n >= 1e12 else "%.0f GB" % (n / 1e9)


def vsan_room(cl, leaving_bytes, min_free_pct):
    """Whether current usage still fits once these hosts leave, with min_free_pct
    left free. Returns (ok, detail), or (None, why) when capacity is unreadable."""
    ds = None
    for d in getattr(cl, "datastore", []) or []:
        if getattr(getattr(d, "summary", None), "type", "") == "vsan":
            ds = d
            break
    if ds is None:
        return None, "no vSAN datastore on %s" % cl.name
    cap = int(getattr(ds.summary, "capacity", 0) or 0)
    free = int(getattr(ds.summary, "freeSpace", 0) or 0)
    if cap <= 0:
        return None, "vSAN datastore reported no capacity"
    used = cap - free
    # A datastore's size is usable bytes; a host's claimed disks are raw. vSAN's
    # on-disk format takes the difference, so scale by the ratio the cluster
    # itself reports rather than subtracting raw bytes from a usable total.
    raw_total = 0
    for h in getattr(cl, "host", []) or []:
        raw_total += vsan_disks(h)[1]
    ratio = (cap / float(raw_total)) if raw_total > 0 else 1.0
    leaving_bytes = leaving_bytes * ratio
    after_cap = cap - leaving_bytes
    after_free = after_cap - used
    pct = (after_free / after_cap * 100.0) if after_cap > 0 else -1.0
    detail = ("vSAN %s used of %s; losing %s leaves %s of %s free (%.0f%%), want %d%%"
              % (gb(used), gb(cap), gb(leaving_bytes), gb(max(after_free, 0)),
                 gb(max(after_cap, 0)), max(pct, 0), min_free_pct))
    return (after_cap > 0 and pct >= min_free_pct), detail


# ------------------------------------------------------------------ phases
# uncluster precedes nsx and release: a TNC-managed host is not deleted from NSX
# directly, it leaves the compute collection and NSX uninstalls. Removing it from
# vCenter first leaves a stale transport node requiring a force delete.

PHASES = ["check", "evacuate", "drain", "disks", "uncluster", "nsx", "release"]


def phase_check(st, ctx):
    r = Result("check")
    if not st["present"]:
        r.add(OK, "%s is not in %s - already released" % (st["fqdn"], ctx["vcenter"]))
        return r
    want = ctx["src"].get("cluster")
    have = st["cluster_name"]
    if want and have and have != want:
        r.add(BLOCKED, "%s is in cluster %s, but the config says %s. Refusing a host "
                       "that is not where the config expects it" % (st["fqdn"], have, want))
        return r
    if st["connection"] != "connected":
        # A disconnected host cannot be read, so an evacuation cannot be checked
        # against it. But release disconnects the host itself on the way out, so
        # one that is already clear of its cluster with no claimed disks has
        # nothing left to verify - blocking there would strand what this tool
        # started.
        if st["cluster"] is not None or st["vsan_disks"]:
            r.add(BLOCKED, "%s is %s in %s and still %s - reconnect it before "
                           "releasing it, or the evacuation cannot be verified"
                  % (st["fqdn"], st["connection"], ctx["vcenter"],
                     "in %s" % st["cluster_name"] if st["cluster"] is not None
                     else "holding vSAN disks"))
            return r
        r.add(OK, "%s is %s, out of its cluster with no claimed disks - only removal "
                  "from inventory is left" % (st["fqdn"], st["connection"]))
        return r
    r.add(OK, "%s connected in %s%s" % (st["fqdn"], have or "the datacenter",
                                        ", maintenance mode" if st["maintenance"] else ""))
    return r


def phase_evacuate(st, ctx):
    """Reports what maintenance mode will relocate. Moves nothing itself."""
    r = Result("evacuate")
    if not st["present"] or st["maintenance"]:
        r.add(OK, "nothing running on %s" % st["fqdn"])
        return r
    on = st["vms_on"]
    if not on:
        r.add(OK, "no powered-on VMs on %s" % st["fqdn"])
        return r
    cl = st["cluster"]
    drs = None
    if cl is not None:
        cfg = getattr(getattr(cl, "configurationEx", None), "drsConfig", None)
        drs = (getattr(cfg, "defaultVmBehavior", None) if cfg and getattr(cfg, "enabled", False) else None)
    names = ", ".join(sorted(vm.name for vm in on)[:4]) + (" (+%d more)" % (len(on) - 4) if len(on) > 4 else "")
    if cl is not None and whole_cluster_leaving(cl, ctx.get("order") or []):
        # No peer host is staying, so DRS has nowhere to put these. vCLS agents
        # are vSphere's own and go when the cluster does; anything else has to
        # be dealt with first.
        agents = [vm for vm in on if str(vm.name).lower().startswith("vcls")]
        others = [vm for vm in on if vm not in agents]
        if others:
            r.add(BLOCKED, "%d powered-on VMs on %s and every host in %s is leaving, so "
                           "there is nowhere to relocate them: %s"
                  % (len(others), st["fqdn"], cl.name,
                     ", ".join(sorted(vm.name for vm in others)[:4])))
        else:
            r.add(OK, "%d vCLS agent VM(s) on %s - vSphere's own, they go with the cluster"
                  % (len(agents), st["fqdn"]))
        return r
    if str(drs) == "fullyAutomated":
        r.add(CHANGE, "%d powered-on VMs on %s will be moved by DRS when it enters "
                      "maintenance mode: %s" % (len(on), st["fqdn"], names))
    else:
        r.add(BLOCKED, "%d powered-on VMs on %s and DRS is %s. Relocate them, or set DRS "
                       "to fully automated: %s"
              % (len(on), st["fqdn"], drs or "off", names))
    return r


def phase_drain(st, ctx, apply_):
    r = Result("drain")
    if not st["present"]:
        r.add(OK, "%s already gone" % st["fqdn"])
        return r
    if st["maintenance"]:
        r.add(OK, "%s in maintenance mode" % st["fqdn"])
        return r
    if st["task"] is not None:
        pct = getattr(st["task"], "progress", None)
        r.add(CHANGE, "%s is entering maintenance mode%s - run again to pick it up"
              % (st["fqdn"], ", %d%%" % pct if pct is not None else ""))
        return r
    if st["vms_on"]:
        cl = st["cluster"]
        cfg = getattr(getattr(cl, "configurationEx", None), "drsConfig", None) if cl is not None else None
        drs = getattr(cfg, "defaultVmBehavior", None) if cfg and getattr(cfg, "enabled", False) else None
        if str(drs) != "fullyAutomated":
            r.add(BLOCKED, "%s still has %d powered-on VMs - see the evacuate phase"
                  % (st["fqdn"], len(st["vms_on"])))
            return r

    # Capacity gate. Runs on a plan as well as a commit, and refuses on an
    # unreadable datastore rather than proceeding.
    dissolving = False
    if st["vsan_disks"]:
        cl = st["cluster"]
        if cl is None:
            r.add(BLOCKED, "%s holds vSAN disks but is not in a cluster; no evacuation "
                           "target" % st["fqdn"])
            return r
        if whole_cluster_leaving(cl, ctx.get("order") or []):
            # Every member is leaving, so the cluster is dissolved rather than
            # shrunk. Nothing remains to receive an evacuation, and whatever is
            # still written to it goes with it.
            dissolving = True
            used = vsan_used(cl)
            left = ("%s still written to it" % gb(used)) if used else "an unreadable amount"
            if not ctx.get("dissolve"):
                r.add(BLOCKED, "all %d hosts of %s are on the release list, so the cluster "
                               "is dissolved, not shrunk. There is nowhere to evacuate to, "
                               "and %s is abandoned with it. Pass --dissolve once that is "
                               "the intent" % (len(cl.host), cl.name, left))
                return r
            r.add(OK, "%s dissolved: all %d hosts leaving, %s abandoned"
                  % (cl.name, len(cl.host), left))
        else:
            room, detail = vsan_room(cl, ctx["leaving_bytes"], ctx["min_free_pct"])
            if room is None:
                r.add(BLOCKED, "cannot read %s capacity to confirm it absorbs the "
                               "evacuation (%s). An evacuation cannot be undone"
                      % (cl.name, detail))
                return r
            if not room:
                r.add(BLOCKED, "%s has insufficient free space: %s. Free capacity, or "
                               "release fewer hosts" % (cl.name, detail))
                return r
            r.add(OK, "capacity checked: %s" % detail)

    if not st["vsan_disks"] or dissolving:
        # Nothing to move: the host holds no vSAN disks, or the whole cluster is
        # going and no peer is left to receive the data.
        mode = "noAction"
        what = ("maintenance mode on %s, no data migration - the cluster is being "
                "dissolved" % st["fqdn"]) if dissolving else "maintenance mode on %s" % st["fqdn"]
    else:
        mode = "evacuateAllData"
        what = ("maintenance mode on %s, full data migration of %s"
                % (st["fqdn"], gb(st["vsan_bytes"])))
    if not apply_:
        r.add(CHANGE, "WOULD enter " + what + " (hours, resumable)")
        return r
    vs = ctx["vs"]
    spec = vs.vim.host.MaintenanceSpec()
    spec.vsanMode = vs.vim.vsan.host.DecommissionMode()
    spec.vsanMode.objectAction = mode
    st["host"].EnterMaintenanceMode_Task(timeout=0, evacuatePoweredOffVms=True, maintenanceSpec=spec)
    r.add(CHANGE, "started " + what + ". Run again to check on it")
    return r


def phase_disks(st, ctx, apply_):
    r = Result("disks")
    if not st["present"] or not st["vsan_disks"]:
        r.add(OK, "no vSAN disks on %s" % st["fqdn"])
        return r
    if not st["maintenance"]:
        r.add(BLOCKED, "%s must be in maintenance mode before its disk groups come out"
              % st["fqdn"])
        return r
    try:
        mappings = list(st["host"].config.vsanHostConfig.storageInfo.diskMapping or [])
    except Exception:
        mappings = []
    if not mappings:
        r.add(OK, "no disk groups on %s" % st["fqdn"])
        return r
    if not apply_:
        r.add(CHANGE, "WOULD remove %d vSAN disk group(s) from %s (%s), rather than "
                      "leaving the metadata for the disk wipe"
              % (len(mappings), st["fqdn"], gb(st["vsan_bytes"])))
        return r
    vs = ctx["vs"]
    sys_ = st["host"].configManager.vsanSystem
    # The spec has to be passed, not just built. Left out, vSAN defaults to
    # ensureObjectAccessibility and tries to relocate components - which hangs
    # when the cluster is being dissolved and there is no peer to take them.
    # By this point drain has either evacuated the data or the operator has
    # accepted losing it, so noAction is right either way.
    spec = vs.vim.host.MaintenanceSpec()
    spec.vsanMode = vs.vim.vsan.host.DecommissionMode()
    spec.vsanMode.objectAction = "noAction"
    done = 0
    for m in mappings:
        try:
            wait_task(sys_.RemoveDiskMapping_Task(mapping=[m], maintenanceSpec=spec),
                      30, "removing a disk group from %s" % st["fqdn"])
            done += 1
        except Exception as e:
            r.add(BLOCKED, "removing a disk group from %s failed: %s" % (st["fqdn"], dv.fault(e)))
            return r
    r.add(CHANGE, "removed %d disk group(s) from %s" % (done, st["fqdn"]))
    return r


def phase_uncluster(st, ctx, apply_):
    """Out of the cluster, to the datacenter level. This is what takes the host out
    of NSX's compute collection and starts the VIB uninstall."""
    r = Result("uncluster")
    if not st["present"]:
        r.add(OK, "%s already gone" % st["fqdn"])
        return r
    if st["cluster"] is None:
        r.add(OK, "%s is already at the datacenter level" % st["fqdn"])
        return r
    if not st["maintenance"]:
        r.add(BLOCKED, "%s must be in maintenance mode before it leaves the cluster"
              % st["fqdn"])
        return r
    if st["vsan_disks"]:
        r.add(BLOCKED, "%s still has claimed vSAN disks - see the disks phase" % st["fqdn"])
        return r
    vs = ctx["vs"]
    dc = None
    p = st["cluster"]
    while p is not None and not isinstance(p, vs.vim.Datacenter):
        p = getattr(p, "parent", None)
    dc = p
    if dc is None:
        r.add(BLOCKED, "cannot find the datacenter above %s" % st["cluster_name"])
        return r
    note = (" NSX will start uninstalling once it leaves the compute collection."
            if st["tn"] is not None else "")
    if not apply_:
        r.add(CHANGE, "WOULD move %s out of %s to %s.%s"
              % (st["fqdn"], st["cluster_name"], dc.name, note))
        return r
    try:
        wait_task(dc.hostFolder.MoveIntoFolder_Task(list=[st["host"]]), 10,
                  "moving %s out of %s" % (st["fqdn"], st["cluster_name"]))
    except Exception as e:
        return r.add(BLOCKED, "moving %s to %s failed: %s"
                     % (st["fqdn"], dc.name, dv.fault(e)))
    r.add(CHANGE, "moved %s out of %s to %s.%s"
          % (st["fqdn"], st["cluster_name"], dc.name, note))
    return r


def phase_nsx(st, ctx, apply_):
    r = Result("nsx")
    nsx = ctx.get("nsx")
    if nsx is None and ctx.get("nsx_error"):
        r.add(BLOCKED, "the source NSX could not be read (%s), so whether it still holds "
                       "%s is unknown" % (ctx["nsx_error"], st["fqdn"]))
        return r
    if nsx is None:
        if st.get("tn") is not None:
            r.add(BLOCKED, "%s is an NSX transport node but no source nsx: is configured - "
                           "add it, or remove the host in NSX by hand first" % st["fqdn"])
        else:
            r.add(SKIP, "no source NSX configured")
        return r
    if st["tn"] is None and not st.get("nsx_left"):
        r.add(OK, "%s is not a transport node" % st["fqdn"])
        return r
    if st["tn"] is None and st.get("nsx_left"):
        r.add(CHANGE, "NSX no longer lists %s, but the host still carries %s: the "
                      "manager-side record is gone, the host-side state is not"
              % (st["fqdn"], ", ".join(st["nsx_left"])))
        return r
    tn = st["tn"]
    ident = tn.get("id") or tn.get("unique_id") or ""
    if st["cluster"] is not None:
        r.add(BLOCKED, "%s is still in %s. It leaves NSX by leaving the compute "
                       "collection - run the uncluster phase first"
              % (st["fqdn"], st["cluster_name"]))
        return r
    if not apply_:
        r.add(CHANGE, "WOULD wait for NSX to finish uninstalling from %s, and force-delete "
                      "transport node %s if it is left behind" % (st["fqdn"], ident))
        return r
    deadline = time.time() + ctx["nsx_wait"]
    while time.time() < deadline:
        if not nsx_node_for(nsx, st["fqdn"]):
            r.add(CHANGE, "NSX released %s" % st["fqdn"])
            return r
        time.sleep(ctx["poll"])
    try:
        path = tn.get("path") or "/api/v1/transport-nodes/%s" % ident
        if path.startswith("/api/"):
            path += "?force=true"       # manager API honours force; policy does not
        nsx.call("DELETE", path)
        r.add(CHANGE, "NSX did not release %s in %ds; force-deleted transport node %s"
              % (st["fqdn"], ctx["nsx_wait"], ident))
    except Exception as e:
        r.add(BLOCKED, "NSX still holds %s and the delete failed: %s. Clear it in NSX "
                       "before removing the host from vCenter" % (st["fqdn"], e))
    return r


def phase_release(st, ctx, apply_):
    r = Result("release")
    if not st["present"]:
        r.add(OK, "%s is out of %s" % (st["fqdn"], ctx["vcenter"]))
        return r
    if st["cluster"] is not None:
        r.add(BLOCKED, "%s is still in %s - see the uncluster phase" % (st["fqdn"], st["cluster_name"]))
        return r
    if st["tn"] is not None:
        r.add(BLOCKED, "NSX still lists %s as a transport node. Removing it from vCenter "
                       "now leaves a stale node that needs a force delete - see the nsx "
                       "phase" % st["fqdn"])
        return r
    if st.get("nsx_left"):
        # esx_ready has no NSX handling: it removes the TEP vmkernels as extra and
        # leaves the VIBs and N-VDS, which then fail the destination's vLCM
        # solution apply.
        r.add(BLOCKED, "%s still carries %s. esx_ready has no NSX handling and will "
                       "remove the vmkernels while leaving the VIBs. Clear it first:\n"
                       "      python3 nsx_cleanup.py --vcenter %s --host %s --commit\n"
                       "    or once it is out of vCenter:\n"
                       "      python3 nsx_cleanup.py --direct --host %s --commit"
              % (st["fqdn"], ", ".join(st["nsx_left"]), ctx["vcenter"],
                 st["fqdn"], st["fqdn"]))
        return r
    if not st["maintenance"]:
        r.add(BLOCKED, "%s is not in maintenance mode" % st["fqdn"])
        return r
    if not apply_:
        r.add(CHANGE, "WOULD disconnect %s and remove it from %s, leaving it standalone "
                      "for esx_ready" % (st["fqdn"], ctx["vcenter"]))
        return r
    vs = ctx["vs"]
    try:
        wait_task(st["host"].DisconnectHost_Task(), 10, "disconnecting %s" % st["fqdn"])
    except Exception:
        pass        # already disconnected is fine; the destroy below is what matters
    try:
        wait_task(destroy_target(st["host"]).Destroy_Task(), 10,
                  "removing %s from inventory" % st["fqdn"])
    except Exception as e:
        return r.add(BLOCKED, "removing %s from %s failed: %s"
                     % (st["fqdn"], ctx["vcenter"], dv.fault(e)))
    r.add(CHANGE, "removed %s from %s; standalone, ready for esx_ready"
          % (st["fqdn"], ctx["vcenter"]))
    return r


def wait_task(task, minutes, what, clock=time):
    """Wait for a vCenter task, with a deadline.

    deploy_vc's wait has none: a task that never leaves "running" spins until
    somebody notices. Every call here is an operation that either completes or
    is wedged, and a wedged one should say so rather than look like progress.
    """
    deadline = clock.time() + minutes * 60
    while getattr(task.info, "state", "") in ("queued", "running"):
        if clock.time() > deadline:
            pct = getattr(task.info, "progress", None)
            raise TimeoutError(
                "%s is still running after %d minutes%s. The task is in vCenter - "
                "check it there. Ctrl-C is safe: this tool keeps no state of its own"
                % (what, minutes, "" if pct is None else " (%d%%)" % pct))
        clock.sleep(2)
    if getattr(task.info, "state", "") != "success":
        raise dv.TaskError(task.info.error)
    return task.info.result


def destroy_target(host):
    """The object whose Destroy_Task removes this host from vCenter.

    In a cluster that is the HostSystem itself. Once the host is at the
    datacenter level it is a standalone host, which vCenter models as a
    ComputeResource wrapping it, and the HostSystem refuses Destroy_Task with
    "The operation is not supported on the object" - the ComputeResource is
    what goes.
    """
    parent = getattr(host, "parent", None)
    kind = type(parent).__name__ if parent is not None else ""
    if "ComputeResource" in kind and "Cluster" not in kind:
        return parent
    return host


def nsx_node_for(nsx, fqdn):
    short = fqdn.split(".")[0].lower()
    for node in nsx.transport_nodes():
        nm = (node.get("display_name") or "").lower()
        if nm == fqdn.lower() or nm.split(".")[0] == short:
            return node
    return None


# ------------------------------------------------------------------ driver

def run(ctx, apply_, only=None):
    """Every host, every phase, in order. Stops a host at its first blocker or at
    the phase that needs time, and reports the lot."""
    results = {}
    draining = [h for h in ctx["order"] if ctx["states"][h].get("task") is not None]
    for fqdn in ctx["order"]:
        st = ctx["states"][fqdn]
        rows = []
        for ph in PHASES:
            if only and ph not in only:
                continue
            if ph == "check":
                r = phase_check(st, ctx)
            elif ph == "evacuate":
                r = phase_evacuate(st, ctx)
            elif ph == "drain":
                # One evacuation at a time: they draw on the same free space.
                busy = [o for o in draining if o != fqdn]
                if (apply_ and busy and len(busy) >= ctx["parallel"]
                        and st.get("present") and not st.get("maintenance")):
                    r = Result("drain").add(SKIP, "holding off - %s is still draining "
                                                  "(--parallel %d)" % (busy[0], ctx["parallel"]))
                else:
                    r = phase_drain(st, ctx, apply_)
                    if apply_ and r.state == CHANGE and not st.get("maintenance"):
                        draining.append(fqdn)
            elif ph == "disks":
                r = phase_disks(st, ctx, apply_)
            elif ph == "uncluster":
                r = phase_uncluster(st, ctx, apply_)
            elif ph == "nsx":
                r = phase_nsx(st, ctx, apply_)
            else:
                r = phase_release(st, ctx, apply_)
            rows.append(r)
            if r.state == BLOCKED or r.state == SKIP:
                break
            if r.state == CHANGE:
                if apply_ and ph == "drain":
                    # The task is only started, not finished. Everything after it
                    # waits for the next run.
                    break
                # The state is read once at the top of the run. A phase that has
                # just changed something - for real on a commit, hypothetically on
                # a plan - has moved it on, and the phases after it read the same
                # dict. Without this a committed disk removal is invisible to
                # uncluster, which then blocks on disks that are already gone.
                advance(st, ph)
        results[fqdn] = rows
    return results


def advance(st, phase):
    """Move the in-memory state on as if this phase had run.

    Used after a phase changes something, on a plan and on a commit alike: the
    later phases in the same run read this dict, not the live inventory.
    """
    if phase == "drain":
        st["maintenance"] = True
        st["vms_on"] = []
    elif phase == "disks":
        st["vsan_disks"], st["vsan_bytes"] = [], 0
    elif phase == "uncluster":
        st["cluster"], st["cluster_name"] = None, None
    elif phase == "nsx":
        st["tn"] = None
        st["nsx_left"] = []


def report(results, apply_, subset=None):
    worst = OK
    print()
    if subset:
        print("  --hosts limits this run to: %s"
              % ", ".join(h.split(".")[0] for h in subset))
        print()
    for fqdn, rows in results.items():
        print("==== %s" % fqdn)
        for r in rows:
            for state, detail in r.rows:
                mark = {OK: "  ok   ", ADD: "  add  ", CHANGE: " change", BLOCKED: " BLOCK ", SKIP: "  skip "}[state]
                print("%s %-10s %s" % (mark, r.step, detail))
            if r.state == BLOCKED:
                worst = BLOCKED
            elif r.state in (ADD, CHANGE) and worst != BLOCKED:
                worst = CHANGE
    print()
    print("==== result")
    print("%-34s %s" % ("HOST", "NEXT"))
    done = 0
    for fqdn, rows in results.items():
        last = rows[-1] if rows else None
        states = [s for r in rows for s, _ in r.rows]
        if all(s == OK for s in states) and any(r.step == "release" for r in rows):
            print("%-34s released - ready for esx_ready" % fqdn)
            done += 1
            continue
        if last is None:
            print("%-34s nothing to do" % fqdn)
            continue
        blocked = [d for r in rows for s, d in r.rows if s == BLOCKED]
        if blocked:
            print("%-34s BLOCKED: %s" % (fqdn, blocked[0][:90]))
        else:
            pend = [(r.step, d) for r in rows for s, d in r.rows if s in (CHANGE, ADD)]
            if pend:
                print("%-34s %s" % (fqdn, ("%s: %s" % pend[0])[:90]))
                continue
            held = [d for r in rows for s, d in r.rows if s == SKIP]
            print("%-34s %s" % (fqdn, held[0][:90] if held else "nothing to do"))
    print()
    if not apply_:
        print("Nothing was changed. Add --commit to apply.")
    return 0 if worst != BLOCKED else 1


def survey(ctx):
    print()
    if ctx.get("subset"):
        print("  --hosts limits this run to %d of the site's hosts; the capacity check "
              "counts only these as leaving" % len(ctx["subset"]))
    print("==== %s" % ctx["vcenter"])
    print("%-28s %-16s %-6s %-6s %-9s %s"
          % ("HOST", "CLUSTER", "MAINT", "VMS", "VSAN", "NSX"))
    total = 0
    for fqdn in ctx["order"]:
        st = ctx["states"][fqdn]
        if not st["present"]:
            print("%-30s %s" % (fqdn, "not in this vCenter - already released?"))
            continue
        total += st["vsan_bytes"]
        if st["tn"] is not None:
            nsx_col = "prepared" + (" +%d on host" % len(st["nsx_left"]) if st["nsx_left"] else "")
        elif st["nsx_left"]:
            nsx_col = "LEFTOVER: " + ", ".join(st["nsx_left"])
        else:
            nsx_col = "-"
        print("%-28s %-16s %-6s %-6s %-9s %s"
              % (fqdn[:28], (st["cluster_name"] or "-")[:16],
                 "yes" if st["maintenance"] else "no",
                 "%d/%d" % (len(st["vms_on"]), len(st["vms_all"])),
                 gb(st["vsan_bytes"]) if st["vsan_disks"] else "-",
                 nsx_col))
    print()
    clusters = {st["cluster"] for st in ctx["states"].values() if st.get("cluster") is not None}
    for cl in clusters:
        room, detail = vsan_room(cl, total, ctx["min_free_pct"])
        verdict = {True: "ok", False: "NOT ENOUGH ROOM", None: "cannot read"}[room]
        print("  %s: %s -> %s" % (cl.name, detail, verdict))
    if total:
        print("  releasing all %d hosts removes %s of vSAN capacity"
              % (len(ctx["order"]), gb(total)))
    print()
    return 0


# ------------------------------------------------------------------ main

def connect(src, site, scfg):
    """Sign in to the source vCenter, and its NSX when one is configured."""
    vc = src["vcenter"]
    user = src.get("user") or "administrator@vsphere.local"
    # Secrets reads keys relative to the site and asks for a blank one by name.
    pw = dv.Secrets(scfg, site, lambda k: vc).get("source.password", "%s on %s" % (user, vc))
    try:
        vs = dv.VSphere(vc, user, pw)
    except Exception as e:
        die("cannot sign in to %s as %s: %s" % (vc, user, dv.fault(e)))
    nsx, nsx_error = None, None
    if src.get("nsx"):
        nuser = src.get("nsx_user") or "admin"
        npw = dv.Secrets(scfg, site, lambda k: src["nsx"]).get(
            "source.nsx_password", "%s on %s" % (nuser, src["nsx"]))
        import nsx_prep
        # A source NSX that cannot be reached does not stop a read-only survey,
        # but it does stop an apply: the nsx phase has nothing to check against.
        try:
            nsx = nsx_prep.Nsx(src["nsx"], nuser, npw)
        except Exception as e:
            nsx_error = "%s: %s" % (src["nsx"], e)
            print("  warning: cannot reach the source NSX %s" % nsx_error)
    return vs, nsx, nsx_error


def pick_hosts(all_hosts, wanted):
    """The subset named on the command line, matched on short name or FQDN."""
    want = [w.strip().lower() for w in wanted.split(",") if w.strip()]
    chosen, unknown = [], []
    for w in want:
        hit = [h for h in all_hosts
               if h.lower() == w or h.split(".")[0].lower() == w.split(".")[0].lower()]
        if hit:
            chosen.append(hit[0])
        else:
            unknown.append(w)
    if unknown:
        die("--hosts names %s, which this site does not list. It has: %s"
            % (", ".join(unknown), ", ".join(h.split(".")[0] for h in all_hosts)))
    seen, out = set(), []
    for h in chosen:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def build_context(args):
    cfg = load_config(args.config)
    src = source_config(cfg, args.site)
    subset = None
    if getattr(args, "hosts", None):
        subset = pick_hosts(src["hosts"], args.hosts)
        src = dict(src)
        src["hosts"] = subset
    vs, nsx, nsx_error = connect(src, args.site, (cfg.get("sites") or {})[args.site])
    nodes = []
    if nsx is not None:
        try:
            nodes = nsx.transport_nodes()
        except Exception as e:
            nsx_error = "%s: %s" % (src["nsx"], e)
            print("  warning: cannot read transport nodes from %s" % nsx_error)
            nsx = None
    states = {}
    for fqdn in src["hosts"]:
        states[fqdn] = host_state(vs, fqdn, nodes)
    leaving = sum(s["vsan_bytes"] for s in states.values() if s.get("present"))
    return {
        "vs": vs, "nsx": nsx, "nsx_error": nsx_error, "src": src, "site": args.site,
        "vcenter": src["vcenter"], "order": src["hosts"], "states": states,
        "leaving_bytes": leaving, "subset": subset,
        "min_free_pct": int(src.get("min_free_pct") or DEFAULT_MIN_FREE_PCT),
        "dissolve": bool(getattr(args, "dissolve", False)),
        "parallel": (max(1, int(args.parallel)) if getattr(args, "parallel", None)
                     else (len(src["hosts"]) if getattr(args, "dissolve", False) else 1)),
        "nsx_wait": int(getattr(args, "nsx_wait", 600) or 600),
        "poll": 15,
    }


def ensure_pyvmomi():
    """Re-run under a Python that has pyvmomi, as deploy_vc does for itself."""
    try:
        import pyVmomi                                           # noqa: F401
        return None
    except ImportError:
        pass
    if os.environ.get("HOST_RELEASE_NO_REEXEC"):
        return None
    for python in dv.candidate_pythons():
        if dv._has_pyvmomi(python):
            print("pyvmomi is not in %s - re-running under %s\n" % (sys.executable, python))
            os.environ["HOST_RELEASE_NO_REEXEC"] = "1"
            os.execv(python, [python, os.path.abspath(__file__)] + sys.argv[1:])
    return ("no Python on this machine has pyvmomi. One command fixes it for good:\n"
            "  python3 %s bootstrap" % os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "deploy_vc.py"))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="host_release.py",
        description="Release ESXi hosts from a source vCenter so esx_ready can prep them.")
    p.add_argument("command", choices=["survey", "plan", "apply", "verify", "example", "selftest"])
    p.add_argument("--config")
    p.add_argument("--site")
    p.add_argument("--commit", action="store_true", help="actually change things")
    p.add_argument("--only", help="comma-separated phases: " + " ".join(PHASES))
    p.add_argument("--dissolve", action="store_true",
                   help="the source cluster is being taken apart, not shrunk. Needed when "
                        "every host in it is on the release list: there is nowhere to "
                        "evacuate to, so maintenance mode takes no action and whatever is "
                        "still on the cluster is abandoned")
    p.add_argument("--hosts", help="act on only these of the site's hosts, comma "
                   "separated, short name or FQDN. The capacity check counts only "
                   "these as leaving, so a cluster that cannot lose all of them may "
                   "still be able to lose some")
    p.add_argument("--parallel", type=int, default=None,
                   help="hosts draining at once. Default 1, because an evacuation draws on "
                        "the free space the other hosts share. With --dissolve no data "
                        "moves, so the default is every host at once")
    p.add_argument("--nsx-wait", dest="nsx_wait", type=int, default=600,
                   help="seconds to wait for NSX to uninstall before force-deleting")
    p.add_argument("--version", action="version", version="host_release.py " + VERSION)
    args = p.parse_args(argv)

    if args.command == "example":
        print(EXAMPLE)
        return 0
    if args.command == "selftest":
        return selftest()
    if not args.config or not args.site:
        die("--config and --site are required for %s" % args.command)

    only = None
    if args.only:
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        bad = [s for s in only if s not in PHASES]
        if bad:
            die("unknown phase(s): %s. Known: %s" % (", ".join(bad), " ".join(PHASES)))

    missing = ensure_pyvmomi()      # may replace this process; never returns if it does
    if missing:
        die(missing)

    ctx = build_context(args)
    if args.command == "survey":
        return survey(ctx)
    if args.command == "verify":
        left = [h for h in ctx["order"] if ctx["states"][h]["present"]]
        for h in ctx["order"]:
            print("  %-32s %s" % (h, "still in %s" % ctx["vcenter"] if h in left else "released"))
        print()
        print("%d of %d released" % (len(ctx["order"]) - len(left), len(ctx["order"])))
        return 1 if left else 0

    apply_ = args.command == "apply" and args.commit
    if args.command == "apply" and not args.commit:
        print("No --commit, so this is a plan.")
    return report(run(ctx, apply_, only), apply_, ctx.get("subset"))


# ------------------------------------------------------------------ selftest

def selftest():
    """Stubs, no vCenter. Checks the gates that stop this tool destroying things."""
    checks = [0, 0]

    def ck(name, cond, saw=None):
        checks[0] += 1
        if not cond:
            checks[1] += 1
            print("FAIL %s%s" % (name, "" if saw is None else "  <- %r" % (saw,)))

    def caught(fn):
        """die() prints to stderr then exits with a code, so read the stream."""
        import io, contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                fn()
        except SystemExit:
            pass
        except Exception as e:
            return str(e)
        return buf.getvalue()

    class N:                                     # a tiny attribute bag
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Spec:
        def __init__(self, *a, **kw):
            pass

    started = []

    def vim_stub():
        """Just enough pyVmomi shape for the apply path to run without a vCenter."""
        return N(Datacenter=Datacenter,
                 HostSystem=type("HostSystem", (), {}),
                 host=N(MaintenanceSpec=Spec),
                 vsan=N(host=N(DecommissionMode=Spec)))

    def host_stub(name):
        return N(EnterMaintenanceMode_Task=lambda **kw: started.append(name))

    class Datacenter(N):
        pass

    class Cluster(N):
        pass

    def ds(cap, free):
        return N(summary=N(type="vsan", capacity=cap, freeSpace=free))

    def cluster(name="cl", cap=100e12, free=60e12, drs="fullyAutomated"):
        return Cluster(name=name, datastore=[ds(cap, free)],
                       configurationEx=N(drsConfig=N(enabled=drs is not None,
                                                     defaultVmBehavior=drs)))

    def state(**kw):
        base = dict(fqdn="esx1.example.com", present=True, host=N(),
                    cluster=None, cluster_name=None, connection="connected",
                    maintenance=False, vms_on=[], vms_all=[],
                    vsan_disks=[], vsan_bytes=0, task=None, tn=None, nsx_left=[])
        base.update(kw)
        return base

    def ctx(**kw):
        base = dict(vs=N(vim=vim_stub()), nsx=None, src={}, order=[],
                    vcenter="oldvc.example.com", leaving_bytes=0,
                    min_free_pct=DEFAULT_MIN_FREE_PCT, parallel=1, nsx_wait=600, poll=0)
        base.update(kw)
        return base

    def states_of(r):
        return [s for s, _ in r.rows]

    # ---- config
    cfg = {"sites": {"s": {"source": {"vcenter": "vc", "hosts": ["a", "b"]}}}}
    ck("config: a good source block parses", source_config(cfg, "s")["hosts"] == ["a", "b"])
    ck("config: a bare string host becomes a list",
       source_config({"sites": {"s": {"source": {"vcenter": "v", "hosts": "only"}}}}, "s")["hosts"] == ["only"])
    ck("config: a missing source block names the site",
       "source" in caught(lambda: source_config({"sites": {"s": {}}}, "s")))
    ck("config: an unknown site is refused",
       "no site" in caught(lambda: source_config(cfg, "nope")))
    ck("config: a typo'd key is caught and a near match offered",
       "vcenter" in caught(lambda: source_config(
           {"sites": {"s": {"source": {"vcentre": "v", "hosts": ["a"]}}}}, "s")))
    ck("config: hosts is required",
       "hosts" in caught(lambda: source_config({"sites": {"s": {"source": {"vcenter": "v"}}}}, "s")))
    ck("sentinel: [set] is recognised as unanswered", unanswered(["set"]) is True)
    ck("sentinel: [verify] too", unanswered(["verify"]) is True)
    ck("sentinel: a real hostname is not", unanswered("vc.example.com") is False)
    ck("sentinel: a real host list is not", unanswered(["a.example.com"]) is False)
    err = caught(lambda: source_config(
        {"sites": {"s": {"source": {"vcenter": ["set"], "cluster": ["set"],
                                    "hosts": ["a.example.com"]}}}}, "s"))
    ck("config: an unanswered [set] is refused, not connected to", "unanswered" in err, err)
    ck("config: and it names every field still open",
       "vcenter" in err and "cluster" in err, err)

    err = caught(lambda: source_config({"sites": {}}, "s"))
    ck("config: a file with no sites: block says so rather than 'Known: none'",
       "not a full env file" in err, err)

    ck("config: a duplicated host is refused",
       "twice" in caught(lambda: source_config(
           {"sites": {"s": {"source": {"vcenter": "v", "hosts": ["a", "a"]}}}}, "s")))

    # ---- the capacity tier is what a vSAN datastore counts
    def _disk(tb):
        return N(capacity=N(block=int(tb * 1e12 // 512), blockSize=512))

    _cache, _cap = _disk(1.92), [_disk(3.84) for _ in range(5)]
    _h = N(configManager=N(vsanSystem=N(QueryDisksForVsan=lambda: [
               N(state="inUse", disk=d) for d in [_cache] + _cap])),
           config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[N(ssd=_cache, nonSsd=_cap)]))))
    _claimed, _b = vsan_disks(_h)
    ck("vsan_disks: every claimed disk is listed", len(_claimed) == 6, len(_claimed))
    ck("vsan_disks: but only the capacity tier is counted",
       abs(_b - 5 * 3.84e12) < 1e10, gb(_b))
    ck("vsan_disks: the cache disk is excluded", _b < 20e12, gb(_b))

    _h2 = N(configManager=N(vsanSystem=N(QueryDisksForVsan=lambda: [
                N(state="inUse", disk=d) for d in [_cache] + _cap])), config=N())
    ck("vsan_disks: no readable mapping falls back to every claimed disk",
       abs(vsan_disks(_h2)[1] - (1.92e12 + 5 * 3.84e12)) < 1e10, gb(vsan_disks(_h2)[1]))

    _h3 = N(configManager=N(vsanSystem=N(QueryDisksForVsan=lambda: [])), config=N())
    ck("vsan_disks: a host with none reads zero", vsan_disks(_h3) == ([], 0))

    # ---- capacity arithmetic
    ok_, d = vsan_room(cluster(cap=100e12, free=60e12), 10e12, 25)
    ck("vsan: 40 used of 100, losing 10 -> 50 of 90 free, passes 25%", ok_ is True)

    # raw host bytes are scaled to usable by the ratio the cluster reports
    def _rawhost(tb):
        caps = [_disk(tb)]
        return N(configManager=N(vsanSystem=N(QueryDisksForVsan=lambda: [
                     N(state="inUse", disk=d) for d in caps])),
                 config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[
                     N(ssd=_disk(1.0), nonSsd=caps)]))))
    _cl = Cluster(name="c", host=[_rawhost(20.0) for _ in range(8)],
                  datastore=[ds(140e12, 60e12)])
    ok_, d = vsan_room(_cl, 20e12, 25)
    ck("vsan: a host's raw bytes are scaled by the cluster's own overhead ratio",
       "losing 17.5 TB" in d, d)
    _cl2 = Cluster(name="c", host=[], datastore=[ds(100e12, 60e12)])
    ok_, d = vsan_room(_cl2, 10e12, 25)
    ck("vsan: no readable hosts means no scaling, not a crash", "losing 10.0 TB" in d, d)
    ok_, d = vsan_room(cluster(cap=100e12, free=50e12), 40e12, 25)
    ck("vsan: 50 used of 100, losing 40 -> 10 of 60 free, fails 25%", ok_ is False)
    ok_, d = vsan_room(cluster(cap=100e12, free=10e12), 95e12, 25)
    ck("vsan: losing more than the cluster holds fails, it does not crash", ok_ is False)
    ok_, d = vsan_room(Cluster(name="c", datastore=[]), 1e12, 25)
    ck("vsan: no vSAN datastore reads as unknown, not as pass", ok_ is None)
    ck("vsan: the detail line shows the numbers it judged on", "want 25%" in d or "no vSAN" in d)

    # ---- check
    ck("check: a host already gone is ok",
       states_of(phase_check(state(present=False), ctx())) == [OK])
    ck("check: a host in the wrong cluster is refused",
       BLOCKED in states_of(phase_check(state(cluster_name="other"),
                                        ctx(src={"cluster": "expected"}))))

    ck("check: disconnected while still in a cluster is refused",
       BLOCKED in states_of(phase_check(
           state(connection="notResponding", cluster=cluster(), cluster_name="cl"), ctx())))
    ck("check: disconnected while still holding vSAN disks is refused",
       BLOCKED in states_of(phase_check(
           state(connection="disconnected", vsan_disks=[1]), ctx())))
    r = phase_check(state(connection="disconnected"), ctx())
    ck("check: disconnected, out of its cluster, no disks - allowed to finish",
       states_of(r) == [OK], r.summary)
    ck("check: and it says why", "only removal from inventory" in r.summary, r.summary)

    # ---- evacuate
    cl_auto, cl_manual = cluster(drs="fullyAutomated"), cluster(drs="manual")
    ck("evacuate: no VMs is ok",
       states_of(phase_evacuate(state(), ctx())) == [OK])
    ck("evacuate: DRS fully automated will move them",
       CHANGE in states_of(phase_evacuate(
           state(vms_on=[N(name="vm1")], cluster=cl_auto), ctx())))
    ck("evacuate: DRS manual refuses rather than choosing destinations",
       BLOCKED in states_of(phase_evacuate(
           state(vms_on=[N(name="vm1")], cluster=cl_manual), ctx())))
    r = phase_evacuate(state(vms_on=[N(name="v%d" % i) for i in range(9)], cluster=cl_manual), ctx())
    ck("evacuate: a long VM list is truncated, not dumped", "+5 more" in r.summary)

    # ---- drain
    ck("drain: a host in maintenance mode is done",
       states_of(phase_drain(state(maintenance=True), ctx(), False)) == [OK])
    r = phase_drain(state(task=N(progress=48)), ctx(), False)
    ck("drain: an in-flight evacuation reports its progress and resumes", "48%" in r.summary)
    r = phase_drain(state(vsan_disks=[1], vsan_bytes=10e12, cluster=cluster()),
                    ctx(leaving_bytes=10e12), False)
    ck("drain: a plan with room says what it would do", CHANGE in states_of(r))
    ck("drain: the plan names it as the long one", "resumable" in r.summary or "hours" in r.summary)
    r = phase_drain(state(vsan_disks=[1], vsan_bytes=40e12, cluster=cluster(cap=100e12, free=50e12)),
                    ctx(leaving_bytes=40e12), False)
    ck("drain: no room blocks, even on a plan", BLOCKED in states_of(r))
    r = phase_drain(state(vsan_disks=[1], vsan_bytes=1e12, cluster=Cluster(name="c", datastore=[])),
                    ctx(leaving_bytes=1e12), False)
    ck("drain: unreadable capacity blocks rather than passing", BLOCKED in states_of(r))
    ck("drain: and says why it will not guess", "cannot be undone" in r.summary)
    r = phase_drain(state(vsan_disks=[1], vsan_bytes=1e12, cluster=None), ctx(), False)
    ck("drain: vSAN disks with no cluster blocks", BLOCKED in states_of(r))
    # the capacity gate must weigh every host leaving, not just this one
    r = phase_drain(state(vsan_disks=[1], vsan_bytes=10e12, cluster=cluster(cap=100e12, free=50e12)),
                    ctx(leaving_bytes=40e12), False)
    ck("drain: the gate counts ALL the hosts leaving, not just this one",
       BLOCKED in states_of(r))

    # ---- a cluster where every host is leaving
    def _member(nm):
        return N(name=nm + ".example.com")

    _cl4 = Cluster(name="src-cl05",
                   host=[_member("esx41"), _member("esx42"), _member("esx43"), _member("esx44")],
                   datastore=[ds(76.8e12, 75.3e12)],
                   configurationEx=N(drsConfig=N(enabled=True, defaultVmBehavior="fullyAutomated")))
    ALL4 = ["esx41.example.com", "esx42.example.com", "esx43.example.com", "esx44.example.com"]
    ck("dissolve: every member on the list is detected",
       whole_cluster_leaving(_cl4, ALL4) is True)
    ck("dissolve: a subset is not", whole_cluster_leaving(_cl4, ALL4[:2]) is False)
    ck("dissolve: short names match too",
       whole_cluster_leaving(_cl4, ["esx41", "esx42", "esx43", "esx44"]) is True)
    ck("dissolve: a cluster with no readable members is not 'all leaving'",
       whole_cluster_leaving(Cluster(name="c", host=[]), ALL4) is False)
    ck("vsan_used: reads written bytes", abs(vsan_used(_cl4) - 1.5e12) < 1e11, vsan_used(_cl4))

    _st4 = state(fqdn="esx41.example.com", vsan_disks=[1], vsan_bytes=19.2e12, cluster=_cl4)
    _c4 = ctx(order=ALL4, leaving_bytes=76.8e12)
    r = phase_drain(_st4, _c4, False)
    ck("dissolve: without --dissolve it blocks", BLOCKED in states_of(r))
    ck("dissolve: and says the cluster is dissolved, not short of space",
       "dissolved, not shrunk" in r.summary, r.summary)
    ck("dissolve: and names what is abandoned", "1.5 TB" in r.summary, r.summary)
    ck("dissolve: and does not talk about free space",
       "insufficient free space" not in r.summary, r.summary)

    _c4d = ctx(order=ALL4, leaving_bytes=76.8e12, dissolve=True)
    r = phase_drain(_st4, _c4d, False)
    ck("dissolve: with --dissolve it plans", CHANGE in states_of(r))
    ck("dissolve: maintenance mode moves no data", "no data migration" in r.summary, r.summary)
    ck("dissolve: and the plan says why", "being dissolved" in r.summary, r.summary)

    # a partial release of the same cluster still gets the capacity check
    _c4p = ctx(order=ALL4[:2], leaving_bytes=38.4e12, dissolve=True)
    r = phase_drain(_st4, _c4p, False)
    ck("dissolve: --dissolve does not disable the gate on a partial release",
       "dissolved" not in r.summary, r.summary)

    _vcls = state(fqdn="esx43.example.com", cluster=_cl4,
                  vms_on=[N(name="vCLS-abc123")], vms_all=[N()])
    r = phase_evacuate(_vcls, ctx(order=ALL4))
    ck("dissolve: a vCLS agent is not treated as a workload", OK in states_of(r), r.summary)
    ck("dissolve: and is named as vSphere's own", "vCLS agent" in r.summary, r.summary)
    _real = state(fqdn="esx43.example.com", cluster=_cl4,
                  vms_on=[N(name="app01")], vms_all=[N()])
    r = phase_evacuate(_real, ctx(order=ALL4))
    ck("dissolve: a real VM blocks, because nowhere is left to move it",
       BLOCKED in states_of(r), r.summary)
    ck("dissolve: and the block says why", "nowhere to relocate" in r.summary, r.summary)

    # ---- disks
    ck("disks: no vSAN disks is ok",
       states_of(phase_disks(state(), ctx(), False)) == [OK])
    ck("disks: refuses outside maintenance mode",
       BLOCKED in states_of(phase_disks(state(vsan_disks=[1]), ctx(), False)))
    h = N(config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[1, 2]))))
    r = phase_disks(state(vsan_disks=[1], maintenance=True, host=h, vsan_bytes=5e12), ctx(), False)
    ck("disks: plans the removal of each disk group", "2 vSAN disk group" in r.summary)

    # the removal must carry a decommission spec, or vSAN tries to relocate
    seen_spec = {}

    class _VsanSys:
        def RemoveDiskMapping_Task(self, mapping=None, maintenanceSpec=None):
            seen_spec["spec"] = maintenanceSpec
            return N(info=N(state="success", result=None))

    _vsd = N(vim=N(Datacenter=Datacenter, HostSystem=type("H", (), {}),
                   host=N(MaintenanceSpec=lambda: N(vsanMode=None)),
                   vsan=N(host=N(DecommissionMode=lambda: N(objectAction=None)))),
             wait=staticmethod(lambda t: None))
    _hd = N(config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[1]))),
            configManager=N(vsanSystem=_VsanSys()))
    phase_disks(state(vsan_disks=[1], maintenance=True, host=_hd, vsan_bytes=1e12),
                ctx(vs=_vsd), True)
    ck("disks: a decommission spec is passed to the removal",
       seen_spec.get("spec") is not None, seen_spec)
    ck("disks: and it is noAction, so vSAN does not try to relocate",
       getattr(getattr(seen_spec.get("spec"), "vsanMode", None), "objectAction", None)
       == "noAction", seen_spec)

    # ---- uncluster
    dc = Datacenter(name="dc", hostFolder=N())
    cl = cluster(); cl.parent = dc
    ck("uncluster: already at the datacenter level is ok",
       states_of(phase_uncluster(state(maintenance=True), ctx(), False)) == [OK])
    ck("uncluster: refuses outside maintenance mode",
       BLOCKED in states_of(phase_uncluster(state(cluster=cl, cluster_name="cl"), ctx(), False)))
    ck("uncluster: refuses while vSAN disks are still claimed",
       BLOCKED in states_of(phase_uncluster(
           state(cluster=cl, cluster_name="cl", maintenance=True, vsan_disks=[1]), ctx(), False)))
    r = phase_uncluster(state(cluster=cl, cluster_name="cl", maintenance=True), ctx(), False)
    ck("uncluster: plans the move to the datacenter", CHANGE in states_of(r))
    r = phase_uncluster(state(cluster=cl, cluster_name="cl", maintenance=True,
                              tn={"id": "tn-1"}), ctx(), False)
    ck("uncluster: says NSX will uninstall when it leaves the collection",
       "NSX will start uninstalling" in r.summary)

    # ---- nsx
    ck("nsx: not a transport node and no NSX configured is a skip",
       states_of(phase_nsx(state(), ctx(), False)) == [SKIP])
    ck("nsx: a prepared host with no source NSX configured is refused",
       BLOCKED in states_of(phase_nsx(state(tn={"id": "x"}), ctx(), False)))
    ck("nsx: not a transport node is ok once NSX is configured",
       states_of(phase_nsx(state(), ctx(nsx=N()), False)) == [OK])
    ck("nsx: refuses while the host is still in the cluster",
       BLOCKED in states_of(phase_nsx(
           state(tn={"id": "x"}, cluster=cl, cluster_name="cl"), ctx(nsx=N()), False)))

    # ---- release
    ck("release: a host already gone is ok",
       states_of(phase_release(state(present=False), ctx())) == [OK] if False else
       states_of(phase_release(state(present=False), ctx(), False)) == [OK])
    ck("release: refuses while still in a cluster",
       BLOCKED in states_of(phase_release(
           state(cluster=cl, cluster_name="cl", maintenance=True), ctx(), False)))
    r = phase_release(state(tn={"id": "x"}, maintenance=True), ctx(), False)
    ck("release: refuses while NSX still holds the host", BLOCKED in states_of(r))
    ck("release: and explains the stale-node consequence", "stale" in r.summary)
    ck("release: refuses outside maintenance mode",
       BLOCKED in states_of(phase_release(state(), ctx(), False)))
    r = phase_release(state(maintenance=True), ctx(), False)
    ck("release: a clean host plans the removal", CHANGE in states_of(r))
    ck("release: and names esx_ready as what comes next", "esx_ready" in r.summary)

    # ---- host-side NSX residue
    def hostnet(opaque=(), vnics=()):
        return N(config=N(network=N(
            opaqueSwitch=[N(type="nsx.LogicalSwitch", name=o) for o in opaque],
            vnic=[N(device=d, spec=N(netStackInstanceKey=k)) for d, k in vnics])))

    ck("residue: a clean host has none",
       nsx_residue(hostnet(vnics=[("vmk0", "defaultTcpipStack")])) == [])
    ck("residue: an N-VDS is found",
       nsx_residue(hostnet(opaque=["nsxswitch"])) == ["opaque switch nsxswitch"])
    ck("residue: TEP vmkernels on the vxlan stack are found",
       nsx_residue(hostnet(vnics=[("vmk0", "defaultTcpipStack"),
                                  ("vmk10", "vxlan"), ("vmk11", "vxlan")]))
       == ["vmk10 on the vxlan stack", "vmk11 on the vxlan stack"])
    ck("residue: a host with no readable network config does not crash",
       nsx_residue(N()) == [])

    r = phase_nsx(state(tn=None, nsx_left=["opaque switch nsxswitch"]), ctx(nsx=N()), False)
    ck("nsx: the manager forgetting a host it never cleaned is reported",
       CHANGE in states_of(r))
    ck("nsx: and it says which side is stale", "host-side state is not" in r.summary)

    r = phase_release(state(maintenance=True, nsx_left=["vmk10 on the vxlan stack"]),
                      ctx(), False)
    ck("release: refuses a host still carrying NSX", BLOCKED in states_of(r))
    ck("release: and hands over the nsx_cleanup command", "nsx_cleanup.py" in r.summary)
    ck("release: offering both the in-vCenter and standalone forms",
       "--vcenter" in r.summary and "--direct" in r.summary)
    ck("release: and says why esx_ready is not the answer",
       "no NSX handling" in r.summary)

    sp = state(tn={"id": "t"}, nsx_left=["opaque switch x"])
    advance(sp, "nsx")
    ck("advance: nsx clears the host-side residue too", sp["nsx_left"] == [])

    c3 = ctx(order=["w"], parallel=1, nsx=N())
    c3["states"] = {"w": state(fqdn="w", maintenance=True, tn={"id": "t"},
                               nsx_left=["opaque switch x"])}
    blocks = [d for r in run(c3, False)["w"] for st_, d in r.rows if st_ == BLOCKED]
    ck("plan: residue does not phantom-block once the nsx phase has planned it",
       blocks == [], blocks)

    # ---- a host that has already left must not KeyError a later phase
    class _NoHost:
        def __init__(self): self.vim = vim_stub()
        def _all(self, kind): return []
        content = N(taskManager=N(recentTask=[]))

    _gone = host_state(_NoHost(), "esx42.example.com", [])
    _live_keys = {"fqdn", "present", "host", "cluster", "cluster_name", "connection",
                  "maintenance", "vms_on", "vms_all", "vsan_disks", "vsan_bytes",
                  "task", "tn", "nsx_left"}
    ck("state: a released host still carries every key",
       set(_gone) == _live_keys, sorted(_live_keys - set(_gone)))
    ck("state: and reads as absent", _gone["present"] is False)

    # every phase must survive it
    _cg = ctx(order=["esx42.example.com"], nsx=N())
    for _ph, _fn in (("check", lambda: phase_check(_gone, _cg)),
                     ("evacuate", lambda: phase_evacuate(_gone, _cg)),
                     ("drain", lambda: phase_drain(_gone, _cg, False)),
                     ("disks", lambda: phase_disks(_gone, _cg, False)),
                     ("uncluster", lambda: phase_uncluster(_gone, _cg, False)),
                     ("nsx", lambda: phase_nsx(_gone, _cg, False)),
                     ("release", lambda: phase_release(_gone, _cg, False))):
        try:
            _fn(); _ok = True
        except Exception as _e:
            _ok = False; _why = "%s: %s" % (type(_e).__name__, _e)
        ck("state: phase %s handles a released host" % _ph, _ok,
           None if _ok else _why)

    _stale = dict(_gone, tn={"id": "tn-42"})
    r = phase_nsx(_stale, ctx(order=["esx42.example.com"], nsx=N()), False)
    ck("state: a released host NSX still lists is reported", CHANGE in states_of(r), r.summary)

    # ---- a task that never finishes must not spin forever
    class _Clock:
        def __init__(self): self.t = 0.0
        def time(self): return self.t
        def sleep(self, n): self.t += n

    _stuck = N(info=N(state="running", progress=30))
    try:
        wait_task(_stuck, 10, "a wedged task", clock=_Clock())
        _got = ""
    except TimeoutError as e:
        _got = str(e)
    ck("wait: a task stuck in running gives up at the deadline", _got != "")
    ck("wait: and reports how long it waited", "10 minutes" in _got, _got)
    ck("wait: and the progress it stalled at", "30%" in _got, _got)
    ck("wait: and says Ctrl-C is safe", "Ctrl-C is safe" in _got, _got)

    _done = N(info=N(state="success", result="ok"))
    ck("wait: a finished task returns its result",
       wait_task(_done, 10, "x", clock=_Clock()) == "ok")

    _fail = N(info=N(state="error", error="boom"))
    try:
        wait_task(_fail, 10, "x", clock=_Clock()); _raised = False
    except dv.TaskError:
        _raised = True
    ck("wait: a failed task still raises TaskError", _raised)

    # ---- which object Destroy_Task goes to
    class ComputeResource(N): pass
    class ClusterComputeResource(N): pass

    _cr = ComputeResource(name="esx41")
    _standalone = N(name="esx41", parent=_cr)
    ck("release: a standalone host destroys its ComputeResource, not itself",
       destroy_target(_standalone) is _cr)
    _ccr = ClusterComputeResource(name="cl")
    _inclu = N(name="esx41", parent=_ccr)
    ck("release: a clustered host destroys itself", destroy_target(_inclu) is _inclu)
    _orphan = N(name="esx41")
    ck("release: no parent falls back to the host", destroy_target(_orphan) is _orphan)

    _boom = N(name="esx41", parent=ComputeResource(
        name="cr", Destroy_Task=lambda: (_ for _ in ()).throw(RuntimeError("nope"))))
    _boom.DisconnectHost_Task = lambda: N(info=N(state="success", result=None))
    r = phase_release(state(maintenance=True, host=_boom), ctx(vs=N(wait=lambda t: None)), True)
    ck("release: a failed destroy reports instead of raising", BLOCKED in states_of(r), r.summary)

    # ---- the order itself
    ck("order: uncluster comes before nsx, which comes before release",
       PHASES.index("uncluster") < PHASES.index("nsx") < PHASES.index("release"))
    ck("order: drain comes before disks", PHASES.index("drain") < PHASES.index("disks"))

    # ---- the driver: serial draining
    c = ctx(order=["a", "b"], parallel=1)
    c["states"] = {"a": state(fqdn="a", vsan_disks=[1], vsan_bytes=1e12,
                              cluster=cluster(), host=host_stub("a")),
                   "b": state(fqdn="b", vsan_disks=[1], vsan_bytes=1e12,
                              cluster=cluster(), host=host_stub("b"))}
    c["leaving_bytes"] = 2e12
    out = run(c, True, only=["drain"])
    second = [s for s, _ in out["b"][0].rows]
    ck("driver: with --parallel 1 the second host waits its turn", SKIP in second)
    c["parallel"] = 2
    c["states"]["a"]["task"] = None
    out = run(c, True, only=["drain"])
    ck("driver: --parallel 2 lets both start",
       SKIP not in [s for s, _ in out["b"][0].rows])

    # ---- a plan must see its own earlier phases
    c = ctx(order=["z"], parallel=1)
    c["states"] = {"z": state(fqdn="z", maintenance=True, cluster=cl, cluster_name="cl",
                              vsan_disks=[1], vsan_bytes=1e12,
                              host=N(config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[1])))),
                              tn={"id": "tn-z"})}
    c["nsx"] = N()
    out = run(c, False)
    steps = [r.step for r in out["z"]]
    blocks = [(r.step, d) for r in out["z"] for st_, d in r.rows if st_ == BLOCKED]
    ck("plan: a host with disks still claimed plans through to release, no phantom block",
       blocks == [], blocks)
    ck("plan: and it reaches the release phase", "release" in steps, steps)

    c["states"]["z"] = state(fqdn="z", maintenance=True, cluster=cl, cluster_name="cl",
                             vsan_disks=[1], vsan_bytes=1e12,
                             host=N(config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[1])))),
                             tn={"id": "tn-z"})
    out = run(c, False, only=["disks", "uncluster"])
    ck("plan: --only still filters to the named phases",
       [r.step for r in out["z"]] == ["disks", "uncluster"])

    sp = state(maintenance=False, vsan_disks=[1], vsan_bytes=1, cluster=cl, tn={"id": "t"})
    advance(sp, "drain")
    ck("advance: drain marks maintenance mode", sp["maintenance"] is True)
    advance(sp, "disks")
    ck("advance: disks clears the claimed disks", sp["vsan_disks"] == [])
    advance(sp, "uncluster")
    ck("advance: uncluster clears the cluster", sp["cluster"] is None)
    advance(sp, "nsx")
    ck("advance: nsx clears the transport node", sp["tn"] is None)

    # a committed change must be visible to the phases after it
    _seq = []

    class _VsanSys2:
        def RemoveDiskMapping_Task(self, mapping=None, maintenanceSpec=None):
            _seq.append("removed")
            return N(info=N(state="success", result=None))

    def _move(list=None):
        _seq.append("moved")
        return N(info=N(state="success", result=None))

    _dc = Datacenter(name="dc", hostFolder=N(MoveIntoFolder_Task=_move))
    _clv = cluster(); _clv.parent = _dc
    _vs2 = N(vim=N(Datacenter=Datacenter, HostSystem=type("H", (), {}),
                   host=N(MaintenanceSpec=lambda: N(vsanMode=None)),
                   vsan=N(host=N(DecommissionMode=lambda: N(objectAction=None)))),
             wait=staticmethod(lambda t: None))
    _h = N(config=N(vsanHostConfig=N(storageInfo=N(diskMapping=[1]))),
           configManager=N(vsanSystem=_VsanSys2()))
    c5 = ctx(order=["q"], vs=_vs2)
    c5["states"] = {"q": state(fqdn="q", maintenance=True, cluster=_clv, cluster_name="cl",
                               vsan_disks=[1], vsan_bytes=1e12, host=_h)}
    out = run(c5, True, only=["disks", "uncluster"])
    blocks = [d for r in out["q"] for st_, d in r.rows if st_ == BLOCKED]
    ck("apply: a committed disk removal is visible to uncluster", blocks == [], blocks)
    ck("apply: and the move actually ran", "moved" in _seq, _seq)
    ck("apply: the disks were removed first", _seq.index("removed") < _seq.index("moved"), _seq)

    # apply mode must still stop at a started drain
    c2 = ctx(order=["y"], parallel=1)
    c2["states"] = {"y": state(fqdn="y", vsan_disks=[1], vsan_bytes=1e12,
                               cluster=cluster(), host=host_stub("y"))}
    c2["leaving_bytes"] = 1e12
    out = run(c2, True)
    ck("apply: stops at the drain it just started, rather than racing ahead",
       [r.step for r in out["y"]] == ["check", "evacuate", "drain"])

    # ---- the whole path: main -> load_config -> connect -> survey.
    # The unit checks above all start at a ready-made ctx, so nothing exercised
    # config loading or sign-in. That is where the first real run failed.
    import tempfile, io, contextlib, types
    CONF = """
sites:
  site-a:
    cluster:
      name: dest-cl01
    some_key_this_tool_never_heard_of: 7
    hosts:
      - esx13.example.com
    source:
      vcenter: srcvc.example.com
      user: administrator@vsphere.local
      password: secret
      nsx: srcnsx.example.com
      nsx_user: admin
      nsx_password: secret2
      hosts:
        - esx13.example.com
        - esx14.example.com
"""
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    fh.write(CONF); fh.close()

    cfg = load_config(fh.name)
    ck("load: a key this tool does not know is not rejected",
       cfg["sites"]["site-a"]["some_key_this_tool_never_heard_of"] == 7)
    ck("load: the source block still parses", len(cfg["sites"]["site-a"]["source"]["hosts"]) == 2)

    # connect() with the real Secrets, against stub endpoints
    seen = {}

    class FakeVS:
        def __init__(self, host, user, password):
            seen["vc"] = (host, user, password)
            self.vim = vim_stub()
        def _all(self, kind):
            return []

    class FakeNsx:
        def __init__(self, host, user, password, timeout=60):
            seen["nsx"] = (host, user, password)
        def transport_nodes(self):
            return []

    try:
        import nsx_prep as _np
    except ImportError:
        _np = None
    if _np is None:
        print("SKIP the end-to-end survey checks: nsx_prep.py is not beside this script. "
              "The nsx phase needs it too - copy it from nsx-overlay-prep")
        os.unlink(fh.name)
    else:
        real_vs, real_nsx = dv.VSphere, _np.Nsx
        dv.VSphere, _np.Nsx = FakeVS, FakeNsx
        os.environ["HOST_RELEASE_NO_REEXEC"] = "1"
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["survey", "--config", fh.name, "--site", "site-a"])
            text = out.getvalue()
            ck("survey: runs end to end and exits 0", rc == 0, rc)
            ck("connect: signed in to the source vCenter with the config password",
               seen.get("vc") == ("srcvc.example.com", "administrator@vsphere.local", "secret"),
               seen.get("vc"))
            ck("connect: signed in to the source NSX", seen.get("nsx") is not None, seen.get("nsx"))
            ck("survey: names the source vCenter", "srcvc.example.com" in text, text[:200])
            ck("survey: reports a host it cannot find rather than crashing",
               "not in this vCenter" in text, text[:300])

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    main(["survey", "--config", fh.name, "--site", "nope"])
                except SystemExit:
                    pass
            ck("main: an unknown site is named, with the known ones", "no site" in err.getvalue(),
               err.getvalue())
        finally:
            dv.VSphere, _np.Nsx = real_vs, real_nsx
            os.unlink(fh.name)

    # ---- an unreachable source NSX
    c4 = ctx(nsx=None, nsx_error="srcnsx.example.com: connection refused")
    r = phase_nsx(state(tn=None), c4, False)
    ck("nsx: unreachable blocks rather than reading as 'none configured'",
       BLOCKED in states_of(r))
    ck("nsx: and says the state is unknown, not absent", "unknown" in r.summary)
    ck("nsx: with no NSX configured at all it is still a skip",
       states_of(phase_nsx(state(), ctx(), False)) == [SKIP])

    # ---- a blank vcenter with no terminal
    err = caught(lambda: source_config(
        {"sites": {"s": {"source": {"hosts": ["a.example.com"]}}}}, "s"))
    ck("config: a blank vcenter with no tty says so rather than prompting",
       "vcenter" in err, err)

    # ---- --hosts subset
    ALL = ["esx21.example.com", "esx22.example.com", "esx23.example.com", "esx24.example.com"]
    ck("hosts: a short name matches", pick_hosts(ALL, "esx22") == ["esx22.example.com"])
    ck("hosts: an FQDN matches", pick_hosts(ALL, "esx22.example.com") == ["esx22.example.com"])
    ck("hosts: several, in the order given",
       pick_hosts(ALL, "esx23,esx21") == ["esx23.example.com", "esx21.example.com"])
    ck("hosts: a repeat is collapsed", pick_hosts(ALL, "esx21,esx21") == ["esx21.example.com"])
    ck("hosts: whitespace and case are tolerated",
       pick_hosts(ALL, " ESX21 , esx22 ") == ["esx21.example.com", "esx22.example.com"])
    err = caught(lambda: pick_hosts(ALL, "esx99"))
    ck("hosts: a name the site does not list is refused", "esx99" in err, err)
    ck("hosts: and the refusal lists what the site does have", "esx21" in err, err)

    # the capacity gate must weigh only the subset
    import tempfile as _tf
    CONF2 = """
sites:
  s:
    cluster: {name: c}
    hosts: [esx21.example.com]
    source:
      vcenter: vc.example.com
      password: pw
      hosts:
        - esx21.example.com
        - esx22.example.com
        - esx23.example.com
        - esx24.example.com
"""
    fh2 = _tf.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    fh2.write(CONF2); fh2.close()
    cfg2 = load_config(fh2.name)
    ck("hosts: the site lists four", len(source_config(cfg2, "s")["hosts"]) == 4)

    class _A:
        config, site, hosts, parallel, nsx_wait = fh2.name, "s", "esx21,esx22", 1, 600

    class FakeVS2:
        def __init__(self, *a):
            self.vim = vim_stub()
        def _all(self, kind):
            return []
    real = dv.VSphere
    dv.VSphere = FakeVS2
    try:
        c = build_context(_A())
        ck("hosts: build_context keeps only the named two", len(c["order"]) == 2, c["order"])
        ck("hosts: and records the subset", c["subset"] == ["esx21.example.com",
                                                            "esx22.example.com"], c["subset"])
    finally:
        dv.VSphere = real
        os.unlink(fh2.name)

    # a held host must not read as finished
    import io as _io, contextlib as _cl
    _rows = {"a": [Result("drain").add(SKIP, "holding off - b is still draining")]}
    _buf = _io.StringIO()
    with _cl.redirect_stdout(_buf):
        report(_rows, True)
    ck("report: a host waiting its turn is not 'nothing to do'",
       "nothing to do" not in _buf.getvalue(), _buf.getvalue())
    ck("report: it says what it is waiting on", "holding off" in _buf.getvalue(), _buf.getvalue())

    class _P:
        config, site, hosts, nsx_wait = None, "s", None, 600
        parallel, dissolve = None, True
    _src = {"hosts": ["a", "b", "c", "d"]}
    ck("parallel: --dissolve defaults to every host at once",
       (max(1, int(_P.parallel)) if _P.parallel
        else (len(_src["hosts"]) if _P.dissolve else 1)) == 4)
    _P.dissolve = False
    ck("parallel: without --dissolve the default stays serial",
       (max(1, int(_P.parallel)) if _P.parallel
        else (len(_src["hosts"]) if _P.dissolve else 1)) == 1)
    _P.parallel, _P.dissolve = 2, True
    ck("parallel: an explicit value wins over both",
       (max(1, int(_P.parallel)) if _P.parallel
        else (len(_src["hosts"]) if _P.dissolve else 1)) == 2)

    ck("example: the sample config parses against the schema",
       source_config(dv.parse_yaml(EXAMPLE), "my-site")["vcenter"] == "oldvc.example.com")

    print("%d checks, %d failed" % (checks[0], checks[1]))
    return 1 if checks[1] else 0


if __name__ == "__main__":
    sys.exit(main())
