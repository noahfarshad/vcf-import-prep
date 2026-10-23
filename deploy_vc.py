#!/usr/bin/env python3
"""deploy_vc.py - build vSphere 8 clusters outside VCF, ready to import as a workload domain.

  python3 deploy_vc.py build --config env_vc8.yaml [--site NAME] [options]
  python3 deploy_vc.py selftest
  python3 deploy_vc.py bootstrap          # once per machine, if no python here has pyvmomi

    --commit              write. Without it nothing is changed
    --only STEP           one step; repeatable
    --detailed-exitcode   0 nothing pending, 2 pending, 1 blocked
    --precheck            vcsa and upgrade: check without touching anything
    --vendor-precheck     also run vcsa-deploy's own precheck, which on a 9.x upgrade
                          updates VLCM on the source appliance, so it needs --commit
    --set NAME[=VALUE]    set one config value by its full name, then stop
    --outdir DIR          run log (default /tmp/deploy_vc/<timestamp>/console.txt)

Steps:
  dns      forward and reverse records for the vCenter and every host
  esx      each host reachable, build read without logging in
  vcsa     writes the vcsa-deploy spec, runs it when installer: is set
  upgrade  upgrades the appliance to upgrade.target, when one is set
  objects  datacenter and cluster: HA, DRS, EVC
  hosts    each ESX host added to the cluster
  vds      distributed switch, uplinks, portgroups
  vmk      vMotion and vSAN vmkernel adapters from the configured ranges
  mgmt     vmk0 and the uplink carrying it, moved onto the distributed switch
  vsan     vSAN enabled on the cluster
  image    the cluster managed by a vLCM image, seeded from a host already in it
  ssh      SSH on the appliance - the import needs it
  ready    import prerequisites

Not done here: licensing, and the import itself. The uplink carrying vmk0 stays out of
network.pnics: the mgmt step moves it, together with vmk0, in one host transaction.

Passwords come from the config or are asked for by name; never written to the run log.

Needs pyvmomi from the vcsa step on. Run it with any python: if the one you typed does
not have pyvmomi, the tool looks for one that does and re-runs itself under it. If no
python on the machine has it, `bootstrap` makes a virtualenv the tool then finds on its
own. PyYAML optional.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import getpass
import ipaddress
import time
import urllib.parse
import urllib.request

VERSION = "1.0"
# ntp sits right after hosts: it is the first thing that can be set once a host is in
# the vCenter, vSAN wants the clocks together before it forms a cluster, and the VCF
# import refuses a host whose time is unmanaged. Late is how it got missed twice.
STEPS = ["dns", "esx", "vcsa", "upgrade", "objects", "hosts", "ntp", "vds", "vmk", "mgmt",
         "vsan", "image", "ssh", "ready"]

OK, ADD, CHANGE, BLOCKED, SKIP = "ok", "add", "change", "blocked", "skip"


class Result:
    """What one step found, and what it did or would do."""

    def __init__(self, step):
        self.step = step
        self.rows = []          # (state, detail)

    def add(self, state, detail):
        self.rows.append((state, detail))
        return self

    @property
    def state(self):
        for s in (BLOCKED, CHANGE, ADD, SKIP):
            if any(r[0] == s for r in self.rows):
                return s
        return OK

    @property
    def summary(self):
        if not self.rows:
            return "nothing to do"
        bad = [d for s, d in self.rows if s == BLOCKED]
        if bad:
            return bad[0] if len(bad) == 1 else "%s (+%d more)" % (bad[0], len(bad) - 1)
        pending = [d for s, d in self.rows if s in (ADD, CHANGE)]
        if pending:
            return "; ".join(pending[:3]) + ("" if len(pending) <= 3 else " (+%d more)" % (len(pending) - 3))
        return "; ".join(d for _, d in self.rows[:3])


# ---------------------------------------------------------------- configuration
def parse_yaml(text):
    """The documented subset: nested maps by indent, block and inline lists, lists of maps,
    # comments, optional quotes. Used when PyYAML is not installed."""
    root = {}
    stack = [{"indent": -1, "container": root, "parent": None, "key": None}]

    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        if body.startswith("- "):
            while len(stack) > 1 and indent < stack[-1]["indent"]:
                stack.pop()
            frame = stack[-1]
            cont = frame["container"]
            if isinstance(cont, dict):                 # the key's container is really a list
                if cont or frame["parent"] is None:
                    raise ValueError("list item where a mapping was expected: %s" % body)
                cont = []
                frame["parent"][frame["key"]] = cont
                frame["container"] = cont
            item = body[2:].strip()
            if ":" in item and item[0] not in "\"'":
                k, _, v = item.partition(":")
                entry = {}
                if v.strip():
                    entry[k.strip()] = _scalar(v)
                cont.append(entry)
                stack.append({"indent": indent, "container": entry, "parent": None, "key": None})
            else:
                cont.append(_scalar(item))
            continue

        while len(stack) > 1 and indent <= stack[-1]["indent"]:
            stack.pop()
        cont = stack[-1]["container"]
        if not isinstance(cont, dict):
            raise ValueError("mapping key inside a list: %s" % body)
        key, _, val = body.partition(":")
        key = key.strip()
        if val.strip():
            cont[key] = _scalar(val)
        else:
            child = {}
            cont[key] = child
            stack.append({"indent": indent, "container": child, "parent": cont, "key": key})
    return root


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "~", ""):
        return None
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    try:
        return int(v)
    except ValueError:
        return v


def _strip_comment(line):
    """Drop a trailing # comment, but not a # inside quotes."""
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


KNOWN = {
    "installer": None, "outdir": None,
    "upgrade": {"installer": None, "target": None},
    "esx": {"build": None, "user": None},
    "sites": {"*": {
        "vcenter": {"fqdn": None, "ip": None, "prefix": None, "gateway": None, "dns": None,
                    "ntp": None, "sso_domain": None, "size": None, "vc_user": None,
                    "vc_password": None, "root_password": None,
                    "target": {"host": None, "datastore": None, "network": None,
                               "password": None, "appliance_name": None},
                    "temp_ip": None},
        "datacenter": None,
        "cluster": {"name": None, "ha": None, "drs": None, "evc": None, "disk_groups": None,
                    "image_seed_from": None},
        "network": {"vds": None, "mtu": None, "uplinks": None, "pnics": None,
                    "management": {"device": None, "allow_vlan_change": None},
                    "portgroups": {"management": {"name": None, "vlan": None},
                                   "vmotion": {"name": None, "vlan": None},
                                   "vsan": {"name": None, "vlan": None}},
                    # gateway: only for routed vMotion or vSAN. Left out, the adapter
                    # gets none, which is what a single-subnet cluster wants.
                    "vmotion": {"prefix": None, "range": None, "gateway": None},
                    "vsan": {"prefix": None, "range": None, "gateway": None}},
        # ntp: the hosts' time source. Left out, the hosts follow vcenter.ntp, which
        # is what one site normally wants.
        "ntp": None,
        "esx_user": None, "esx_password": None, "hosts": None,
        # source: the vCenter these hosts come OUT of. host_release owns it and
        # validates it strictly; this tool only has to tolerate it so one env
        # file can describe both ends of a migration.
        "source": "*",
        "extra": "*",
    }},
}
PASSWORD_KEYS = ("vcenter.vc_password", "vcenter.root_password", "esx_password")


def check_keys(node, known, path=""):
    """Every name checked against KNOWN: unknown ones come back with the nearest known one."""
    import difflib
    bad = []
    if known == "*" or not isinstance(node, dict):
        return bad
    for key, value in node.items():
        here = "%s.%s" % (path, key) if path else key
        if "*" in known:
            bad += check_keys(value, known["*"], here)
            continue
        if key not in known:
            near = difflib.get_close_matches(key, [k for k in known if k != "*"], 1, 0.4)
            bad.append((here, near[0] if near else ""))
            continue
        if isinstance(known[key], dict):
            bad += check_keys(value, known[key], here)
    return bad


def load_config(path):
    with open(path) as f:
        text = f.read()
    try:
        import yaml
        cfg = yaml.safe_load(text)
    except ImportError:
        cfg = parse_yaml(text)
    if not isinstance(cfg, dict):
        die("%s did not parse as a mapping" % path)
    for name, near in check_keys(cfg, KNOWN):
        die("%s: '%s' is not a name this tool knows%s" % (path, name, " - did you mean %s?" % near if near else ""))
    return cfg


def pick_site(cfg, wanted):
    """The site to build: --site, else the first under sites:, as fleetparity does."""
    sites = cfg.get("sites")
    if not isinstance(sites, dict) or not sites:
        die("the config has no sites: block")
    if wanted is None:
        return list(sites)[0]
    if wanted not in sites:
        die("site %s is not in the config - it has: %s" % (wanted, ", ".join(sites)))
    return wanted


def site_config(cfg, site):
    """One flat view: the site's own values, with the shared ones folded in."""
    merged = dict(cfg["sites"][site] or {})
    for shared in ("installer", "esx", "outdir", "upgrade"):
        if shared in cfg and shared not in merged:
            merged[shared] = cfg[shared]
    if "installer" in merged:
        merged.setdefault("vcenter", {}).setdefault("installer", merged["installer"])
    return merged


class Secrets:
    """A password comes from the config, or is asked for once, named as the config names it."""

    def __init__(self, cfg, site, system_of):
        self.cfg, self.site, self.system_of, self.seen = cfg, site, system_of, {}

    def get(self, key, system=None):
        if key in self.seen:
            return self.seen[key]
        value = opt(self.cfg, key)
        if not value:
            where = "sites.%s.%s" % (self.site, key)
            if not sys.stdin.isatty():
                die("%s is blank and there is no terminal to ask on - put it there with --set %s" % (where, where))
            value = getpass.getpass("      password for %s (%s): " % (system or self.system_of(key), where))
        self.seen[key] = value
        return value


def set_value(path, name, value):
    """Put one value in the config by its full dotted name, changing nothing else."""
    lines = open(path).read().splitlines()
    want = name.split(".")
    stack, target = [], None
    for i, raw in enumerate(lines):
        body = _strip_comment(raw).rstrip()
        if not body.strip() or body.strip().startswith("- "):
            continue
        indent = len(body) - len(body.lstrip())
        key = body.strip().partition(":")[0].strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        here = [k for _, k in stack] + [key]
        stack.append((indent, key))
        if here == want:
            target = i
            break
    if target is None:
        import difflib
        near = difflib.get_close_matches(want[-1], _flat_names(path), 1, 0.3)
        die("%s is not in %s%s" % (name, path, " - did you mean %s?" % near[0] if near else ""))
    head, _, rest = lines[target].partition(":")
    comment = ""
    stripped = _strip_comment(rest)
    if len(stripped) < len(rest):
        comment = rest[len(stripped):]
    lines[target] = "%s: %s%s" % (head, value, comment.rstrip() and "  " + comment.strip() or "")
    backup = path + ".before-set"
    os.replace(path, backup)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return backup


def _flat_names(path):
    out, stack = [], []
    for raw in open(path).read().splitlines():
        body = _strip_comment(raw).rstrip()
        if not body.strip() or body.strip().startswith("- "):
            continue
        indent = len(body) - len(body.lstrip())
        key = body.strip().partition(":")[0].strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        stack.append((indent, key))
        out.append(".".join(k for _, k in stack))
    return out


def unset(value):
    """Not answered yet: blank, or a [set] placeholder - which YAML reads as a one-item list."""
    return value in ("", None, [], {}, "[set]", "[verify]") or (
        isinstance(value, list) and [str(v).strip().lower() for v in value] in (["set"], ["verify"]))


def need(cfg, path, what):
    """Fetch a required dotted key, or stop with a message naming it."""
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            die("%s is not set in the config - %s" % (path, what))
        node = node[part]
    if unset(node):
        die("%s is not answered in the config yet - %s" % (path, what))
    return node


def opt(cfg, path, default=None):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if unset(node) else node


def die(msg):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(2)


def ip_range(spec):
    """'10.0.141.61-99' or '10.0.141.61-10.0.141.99' -> list of addresses."""
    spec = str(spec).strip()
    if "-" not in spec:
        return [spec]
    lo, _, hi = spec.partition("-")
    lo = lo.strip()
    hi = hi.strip()
    if "." not in hi:
        hi = lo.rsplit(".", 1)[0] + "." + hi
    a, b = ipaddress.ip_address(lo), ipaddress.ip_address(hi)
    if b < a:
        die("IP range %s runs backwards" % spec)
    return [str(ipaddress.ip_address(i)) for i in range(int(a), int(b) + 1)]


def netmask(prefix):
    return str(ipaddress.IPv4Network("0.0.0.0/%s" % prefix).netmask)


# ---------------------------------------------------------------- plain checks
def dns_check(name, expect_ip=None):
    """(ok, detail) for forward and reverse resolution."""
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(name, None, socket.AF_INET)})
    except socket.gaierror:
        return False, "%s does not resolve" % name
    if expect_ip and expect_ip not in addrs:
        return False, "%s resolves to %s, config says %s" % (name, ",".join(addrs), expect_ip)
    try:
        rev = socket.gethostbyaddr(addrs[0])[0]
    except OSError:
        return False, "%s has no reverse record for %s" % (name, addrs[0])
    if rev.lower().rstrip(".") != name.lower().rstrip("."):
        return False, "%s reverses to %s" % (name, rev)
    return True, "%s %s forward and reverse" % (name, addrs[0])


SOAP = (
    '<?xml version="1.0" encoding="UTF-8"?><soap:Envelope '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    '<RetrieveServiceContent xmlns="urn:vim25"><_this type="ServiceInstance">'
    "ServiceInstance</_this></RetrieveServiceContent></soap:Body></soap:Envelope>"
)


def soap_about(host, timeout=8):
    """(version, build) a host or vCenter reports on 443, without logging in."""
    req = urllib.request.Request(
        "https://%s/sdk" % host, data=SOAP.encode(),
        headers={"Content-Type": "text/xml", "SOAPAction": "urn:vim25/5.0"})
    ctx = ssl._create_unverified_context()
    try:
        body = urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode("utf-8", "replace")
    except Exception:
        return None, None
    return _tag(body, "version"), _tag(body, "build")


def _tag(xml, name):
    a = xml.find("<%s>" % name)
    if a < 0:
        return None
    b = xml.find("</%s>" % name, a)
    return xml[a + len(name) + 2:b] if b > 0 else None


def thumbprint(host, port=443, timeout=8):
    """The host's SHA-1 certificate thumbprint, as AddHost wants it."""
    ctx = ssl._create_unverified_context()
    with socket.create_connection((host, port), timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    h = hashlib.sha1(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


# ---------------------------------------------------------------- pure diff helpers
def diff_names(wanted, existing):
    """(to_create, already_there), order preserved. Used for portgroups and the like."""
    have = {str(e) for e in existing}
    return [w for w in wanted if w not in have], [w for w in wanted if w in have]


def assign_ips(hosts, pool):
    """Give each host the next free address, stably by position."""
    if len(pool) < len(hosts):
        die("the IP range holds %d addresses for %d hosts" % (len(pool), len(hosts)))
    return dict(zip(hosts, pool))


def vcsa_spec(cfg, target_pw, root_pw, sso_pw):
    """The JSON the VCSA CLI installer takes."""
    vc = cfg["vcenter"]
    tgt = cfg["vcenter"]["target"]
    return {
        "__version": "2.13.0",
        "__comments": "generated by deploy_vc.py %s" % VERSION,
        "new_vcsa": {
            "esxi": {
                "hostname": tgt["host"],
                "username": "root",
                "password": target_pw,
                "deployment_network": tgt["network"],
                "datastore": tgt["datastore"],
            },
            "appliance": {
                "thin_disk_mode": True,
                "deployment_option": vc.get("size", "small"),
                "name": vc["fqdn"].split(".")[0],
            },
            "network": {
                "ip_family": "ipv4",
                "mode": "static",
                "system_name": vc["fqdn"],
                "ip": vc["ip"],
                "prefix": str(vc["prefix"]),
                "gateway": vc["gateway"],
                "dns_servers": list(vc["dns"]) if isinstance(vc["dns"], list) else [vc["dns"]],
            },
            "os": {"password": root_pw, "ntp_servers": _as_list(vc.get("ntp", [])), "ssh_enable": True},
            "sso": {"password": sso_pw, "domain_name": vc.get("sso_domain", "vsphere.local")},
        },
        "ceip": {"settings": {"ceip_enabled": False}},
    }


def _as_list(v):
    if v in (None, "", []):
        return []
    return list(v) if isinstance(v, list) else [v]


class TaskError(RuntimeError):
    """A failed task, carrying its fault object rather than just the message.

    The message alone is often the useless outer wrapper - DvsOperationBulkFault says
    only "one or more host members" and keeps the real reason in hostFault.
    """

    def __init__(self, error):
        self.error = error
        RuntimeError.__init__(self, str(getattr(error, "msg", None) or error))


def fault(e):
    """vmodl faults print as a multi-line object dump; take the one line that matters."""
    e = getattr(e, "error", None) or e                 # unwrap a TaskError
    # A bulk fault's own message names no host and no cause; the first host's does.
    for fh in (getattr(e, "hostFault", None) or []):
        inner = getattr(fh, "fault", None)
        if inner is not None:
            host = getattr(getattr(fh, "host", None), "name", None)
            return "%s%s" % ("%s: " % host if host else "", fault(inner))
    msg = getattr(e, "msg", None)
    if not msg:
        fm = getattr(e, "faultMessage", None) or []
        msg = "; ".join(getattr(m, "message", str(m)) for m in fm) or None
    text = str(msg or e).strip()
    first = text.splitlines()[0] if text else e.__class__.__name__
    if first.endswith("{"):                            # the dump's opening line
        first = first.rstrip(" {")
    # InvalidArgument puts the whole of its meaning in invalidProperty. Without it the
    # message is "A specified parameter was not correct:" and literally nothing else.
    bad = getattr(e, "text", None) or getattr(e, "invalidProperty", None)
    if bad:
        first = ("%s %s" % (first.rstrip(), bad) if first.rstrip().endswith(":")
                 else "%s: %s" % (first, bad))
    return first[:300]


class Vapi:
    """The vCenter Automation (REST) API, for the few things SOAP cannot reach.

    vSphere Lifecycle Manager lives here and not in the SOAP API: a cluster's image
    management is turned on through esx/settings, which pyVmomi has no binding for.
    """

    def __init__(self, host, user, password, timeout=30):
        self.base = "https://%s/api" % host
        self.ctx = ssl._create_unverified_context()
        req = urllib.request.Request(self.base + "/session", method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(
            ("%s:%s" % (user, password)).encode()).decode())
        with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
            self.sid = json.loads(r.read().decode())

    def call(self, method, path, body=None, timeout=300):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data)
        req.add_header("vmware-api-session-id", self.sid)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode()
            except Exception:
                pass
            # vAPI answers a refusal with a structured fault. Read the sentences out
            # of it; a whole serialised type signature on the console helps nobody.
            nice = ""
            try:
                nice = vapi_message(json.loads(raw))
            except Exception:
                pass
            raise RuntimeError("%s %s: %s %s" % (method, path, e.code, nice or raw[:400]))

    def task(self, method, path, body=None, minutes=60):
        """Run an operation as a task and wait for it.

        Appending vmw-task=true turns one of these calls into a task id; the work
        then happens behind /cis/tasks, and the HTTP call returning 200 says only
        that vCenter accepted the job.
        """
        tid = self.call(method, path + ("&" if "?" in path else "?") + "vmw-task=true", body)
        if not isinstance(tid, str):           # some builds answer synchronously
            return tid
        deadline = time.time() + minutes * 60
        while True:
            info = self.call("GET", "/cis/tasks/%s" % urllib.parse.quote(tid)) or {}
            status = info.get("status")
            if status == "SUCCEEDED":
                return info.get("result")
            if status in ("FAILED", "BLOCKED"):
                raise RuntimeError(vapi_message(info) or "%s %s: %s" % (method, path, status))
            if time.time() > deadline:
                raise RuntimeError("%s %s: still %s after %d minutes"
                                   % (method, path, status, minutes))
            time.sleep(10)


def vapi_message(node, want=("ERROR",)):
    """The human-readable lines out of a vAPI result, notification list or error.

    These answers nest localizable messages several layers down and under half a
    dozen different key names, so walk the structure rather than guess the shape.
    """
    out, seen = [], set()

    def walk(n, depth=0):
        if depth > 8:
            return
        if isinstance(n, dict):
            text = n.get("default_message") or n.get("message")
            if isinstance(text, dict):
                text = text.get("default_message")
            if isinstance(text, str) and text.strip() and text not in seen:
                seen.add(text)
                out.append(text.strip())
            for key in ("error", "result", "notifications", "errors", "warnings",
                        "info", "messages", "data", "value", "status", "checks",
                        "check_statuses", "host_statuses"):
                if key in n:
                    walk(n[key], depth + 1)
            if not any(k in n for k in ("error", "result", "notifications")):
                for v in n.values():
                    if isinstance(v, (dict, list)):
                        walk(v, depth + 1)
        elif isinstance(n, list):
            for v in n:
                walk(v, depth + 1)

    walk(node)
    return "; ".join(out)[:1200]


# ---------------------------------------------------------------- vSphere layer
class VSphere:
    """Thin, idempotent wrapper over pyVmomi. Every ensure_* returns (state, detail)."""

    def __init__(self, host, user, password):
        try:
            from pyVim.connect import SmartConnect
            from pyVmomi import vim
        except ImportError:
            # Getting here means the re-exec in ensure_pyvmomi() found nothing either.
            die("pyvmomi is not installed for %s, and no other Python here has it.\n"
                "One command fixes it for good:\n  python3 %s bootstrap"
                % (sys.executable, os.path.abspath(__file__)))
        self.vim = vim
        ctx = ssl._create_unverified_context()
        self.si = SmartConnect(host=host, user=user, pwd=password, sslContext=ctx, disableSslCertValidation=True)
        self.content = self.si.RetrieveContent()
        self._rest = None
        self._rest_args = (host, user, password)

    def vapi(self):
        """The REST side of the same vCenter, opened once and kept."""
        if self._rest is None:
            self._rest = Vapi(*self._rest_args)
        return self._rest

    def _all(self, kind, root=None):
        view = self.content.viewManager.CreateContainerView(root or self.content.rootFolder, [kind], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def find(self, kind, name, root=None):
        for o in self._all(kind, root):
            if o.name == name:
                return o
        return None

    @staticmethod
    def wait(task):
        import time
        while task.info.state in ("queued", "running"):
            time.sleep(2)
        if task.info.state != "success":
            raise TaskError(task.info.error)
        return task.info.result

    # ---- objects
    def ensure_datacenter(self, name, apply_):
        if self.find(self.vim.Datacenter, name):
            return OK, "datacenter %s" % name
        if not apply_:
            return ADD, "create datacenter %s" % name
        self.content.rootFolder.CreateDatacenter(name=name)
        return ADD, "created datacenter %s" % name

    def seed_host(self, want, build=None):
        """A host already in this vCenter whose running image can seed a new cluster.

        `want` is a host name, or "auto" to take any connected host at the expected
        build. Seeding only works at creation, so the only candidates are hosts
        already in the inventory - for a second cluster on a vCenter that is exactly
        the first cluster's hosts, which run the same image by design.
        """
        if not want:
            return None, ""
        if str(want).lower() != "auto":
            host = self.find(self.vim.HostSystem, str(want))
            if host is None:
                return None, "seed host %s is not in this vCenter" % want
            return host, ""
        for host in self._all(self.vim.HostSystem):
            if getattr(getattr(host, "runtime", None), "connectionState", "") != "connected":
                continue
            got = getattr(getattr(getattr(host, "config", None), "product", None), "build", "")
            if not build or str(got) == str(build):
                return host, ""
        return None, ("no connected host in this vCenter%s to seed an image from"
                      % (" at build %s" % build if build else ""))

    def ensure_cluster(self, dc_name, name, ha, drs, evc, apply_, seed=None, build=None):
        dc = self.find(self.vim.Datacenter, dc_name)
        if dc is None:
            return BLOCKED, "datacenter %s does not exist yet" % dc_name
        existing = self.find(self.vim.ClusterComputeResource, name, dc.hostFolder)
        if existing:
            if seed and not getattr(existing, "lifecycleManaged", False):
                return OK, ("cluster %s - already there, so it cannot be seeded with an "
                            "image now. Seeding only works at creation; use --only image "
                            "--commit to convert it" % name)
            return OK, "cluster %s" % name
        seed_host, why = (self.seed_host(seed, build) if seed else (None, ""))
        if not apply_:
            return ADD, "create cluster %s (DRS %s%s)%s%s" % (
                name, "on" if drs else "off", ", EVC %s" % evc if evc else "",
                "; HA on after vSAN" if ha else "",
                (", on a vLCM image seeded from %s" % seed_host.name if seed_host
                 else ", baseline-managed: %s" % why) if seed else "")
        # Created bare on purpose. CreateClusterEx refuses a dasConfig that carries only
        # `enabled` (InvalidArgument: spec.dasConfig), so HA and DRS go on afterwards,
        # where a refusal costs the settings rather than the cluster.
        spec = self.vim.cluster.ConfigSpecEx()
        notes = []
        if seed and seed_host is not None:
            # The import refuses a baseline-managed cluster, and converting one
            # afterwards is a one-way change that needs a committed software draft
            # first. Born on an image, none of that applies.
            v = self.vim
            spec.hostSeedSpec = v.ComputeResource.HostSeedSpec(
                singleHostSpec=v.ComputeResource.HostSeedSpec.SingleHostSpec(
                    existingHost=seed_host))
        elif seed:
            notes.append("not seeded with an image: %s" % why)
        try:
            cl = dc.hostFolder.CreateClusterEx(name=name, spec=spec)
        except Exception as e:
            if seed and seed_host is not None:
                return BLOCKED, ("cannot create cluster %s seeded from %s: %s. Remove "
                                 "cluster.image_seed_from to create it baseline-managed "
                                 "and convert with --only image --commit instead"
                                 % (name, seed_host.name, fault(e)))
            return BLOCKED, "cannot create cluster %s: %s" % (name, fault(e))
        if seed and seed_host is not None:
            # Say what it actually is, not what was asked for.
            if getattr(cl, "lifecycleManaged", False):
                notes.append("on a vLCM image seeded from %s" % seed_host.name)
            else:
                notes.append("the image seed did not take - it is baseline-managed, "
                             "so --only image --commit is still needed")
        try:
            # HA deliberately off here: vSAN cannot be configured while it is on,
            # and step_vsan turns it on once the datastore has capacity.
            self.wait(cl.ReconfigureComputeResource_Task(self.cluster_spec(False, drs), modify=True))
        except Exception as e:
            notes.append("DRS not set: %s" % fault(e))
        if evc:
            try:
                self.wait(cl.EvcManager().ConfigureEvcMode_Task(evcModeKey=evc))
            except Exception as e:                      # EVC is advisory, never fatal here
                notes.append("EVC %s not set: %s" % (evc, fault(e)))
        if notes:
            return CHANGE, "created cluster %s, %s" % (name, "; ".join(notes))
        return ADD, "created cluster %s" % name

    def cluster_spec(self, ha, drs):
        """A complete HA block. vCenter validates every field of dasConfig, not just enabled."""
        v = self.vim
        spec = v.cluster.ConfigSpecEx(
            drsConfig=v.cluster.DrsConfigInfo(enabled=bool(drs), defaultVmBehavior="fullyAutomated"))
        if not ha:
            spec.dasConfig = v.cluster.DasConfigInfo(enabled=False)
            return spec
        spec.dasConfig = v.cluster.DasConfigInfo(
            enabled=True,
            hostMonitoring="enabled",
            vmMonitoring="vmMonitoringDisabled",
            admissionControlEnabled=True,
            failoverLevel=1,
            admissionControlPolicy=v.cluster.FailoverLevelAdmissionControlPolicy(failoverLevel=1),
            defaultVmSettings=v.cluster.DasVmSettings(restartPriority="medium",
                                                      isolationResponse="none"))
        return spec

    # ---- hosts
    def ensure_host(self, cluster_name, fqdn, password, apply_):
        if self.find(self.vim.HostSystem, fqdn):
            return OK, fqdn
        cl = self.find(self.vim.ClusterComputeResource, cluster_name)
        if cl is None:
            return BLOCKED, "%s: cluster %s does not exist yet" % (fqdn, cluster_name)
        if not apply_:
            return ADD, "add %s" % fqdn
        try:
            tp = thumbprint(fqdn)
        except Exception as e:
            return BLOCKED, "%s: cannot read its certificate (%s)" % (fqdn, fault(e))
        spec = self.vim.host.ConnectSpec(hostName=fqdn, userName="root", password=password,
                                         sslThumbprint=tp, force=True)
        try:
            self.wait(cl.AddHost_Task(spec=spec, asConnected=True))
        except Exception as e:
            return BLOCKED, "%s: %s" % (fqdn, fault(e))
        return ADD, "added %s" % fqdn

    # ---- distributed switch
    def ensure_vds(self, dc_name, name, mtu, uplinks, apply_):
        dc = self.find(self.vim.Datacenter, dc_name)
        if dc is None:
            return BLOCKED, "datacenter %s does not exist yet" % dc_name, None
        dvs = self.find(self.vim.DistributedVirtualSwitch, name, dc.networkFolder)
        if dvs:
            if dvs.config.maxMtu != mtu:
                if not apply_:
                    return CHANGE, "%s MTU %d -> %d" % (name, dvs.config.maxMtu, mtu), dvs
                spec = self.vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec(
                    configVersion=dvs.config.configVersion, maxMtu=mtu)
                self.wait(dvs.ReconfigureDvs_Task(spec))
                return CHANGE, "%s MTU set to %d" % (name, mtu), dvs
            return OK, "%s (MTU %d)" % (name, mtu), dvs
        if not apply_:
            return ADD, "create %s, %d uplinks, MTU %d" % (name, uplinks, mtu), None
        cfg = self.vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec(
            name=name, maxMtu=mtu,
            uplinkPortPolicy=self.vim.DistributedVirtualSwitch.NameArrayUplinkPortPolicy(
                uplinkPortName=["uplink%d" % (i + 1) for i in range(uplinks)]))
        dvs = self.wait(dc.networkFolder.CreateDVS_Task(
            self.vim.DistributedVirtualSwitch.CreateSpec(configSpec=cfg)))
        return ADD, "created %s" % name, dvs

    def ensure_portgroups(self, dvs, wanted, apply_):
        """wanted: {name: vlan}. Returns a list of (state, detail)."""
        out = []
        existing = {pg.name: pg for pg in dvs.portgroup}
        for name, vlan in wanted.items():
            if name in existing:
                out.append((OK, "%s (VLAN %s)" % (name, vlan)))
                continue
            if not apply_:
                out.append((ADD, "create portgroup %s VLAN %s" % (name, vlan)))
                continue
            spec = self.vim.dvs.DistributedVirtualPortgroup.ConfigSpec(
                name=name, type="earlyBinding", numPorts=16,
                defaultPortConfig=self.vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                    vlan=self.vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(
                        vlanId=int(vlan), inherited=False)))
            self.wait(dvs.AddDVPortgroup_Task([spec]))
            out.append((ADD, "created portgroup %s" % name))
        return out

    def mgmt_uplinks(self, host):
        """The pnics carrying the standard switch that the management vmkernel is on."""
        ns = host.configManager.networkSystem
        info = ns.networkInfo
        vmk = next((v for v in info.vnic if v.device == "vmk0"), None)
        if vmk is None or not getattr(vmk, "portgroup", None):
            return set()
        pg = next((p for p in info.portgroup if p.spec.name == vmk.portgroup), None)
        if pg is None:
            return set()
        sw = next((v for v in info.vswitch if v.name == pg.spec.vswitchName), None)
        return set(sw.pnic and [p.rsplit("-", 1)[-1] for p in sw.pnic] or []) if sw else set()

    def ensure_host_on_vds(self, dvs, fqdn, pnics, apply_):
        host = self.find(self.vim.HostSystem, fqdn)
        if host is None:
            return BLOCKED, "%s is not in this vCenter yet" % fqdn
        if any(m.config.host == host for m in dvs.config.host):
            return OK, "%s on %s" % (fqdn, dvs.name)
        # taking the uplink that carries vmk0 would cut the host off: this tool does not
        # migrate the management vmkernel, so that pnic has to stay where it is
        try:
            in_use = self.mgmt_uplinks(host)
        except Exception:
            in_use = set()
        # network.pnics is the end state. Taking the nic that carries vmk0 now would cut
        # the host off, so attach the rest and let the mgmt step bring that one over once
        # the vmkernel is safely on the switch.
        clash = [p for p in pnics if p in in_use]
        take = [p for p in pnics if p not in in_use]
        if not take:
            return BLOCKED, ("%s: every nic in network.pnics (%s) carries the management "
                             "vmkernel - the host needs one free nic to join %s on"
                             % (fqdn, ", ".join(pnics), dvs.name))
        pnics = take
        later = (" - %s follows with vmk0 in the mgmt step" % ", ".join(clash)) if clash else ""
        if not apply_:
            return ADD, "attach %s to %s (%s)%s" % (fqdn, dvs.name, ",".join(pnics), later)
        member = self.vim.dvs.HostMember.ConfigSpec(
            operation="add", host=host,
            backing=self.vim.dvs.HostMember.PnicBacking(
                pnicSpec=[self.vim.dvs.HostMember.PnicSpec(pnicDevice=p) for p in pnics]))
        self.wait(dvs.ReconfigureDvs_Task(
            self.vim.DistributedVirtualSwitch.ConfigSpec(
                configVersion=dvs.config.configVersion, host=[member])))
        return ADD, "attached %s (%s)%s" % (fqdn, ",".join(pnics), later)

    # ---- vmkernel adapters
    @staticmethod
    def vmk_gateway(vnic):
        """The gateway on one vmkernel, or None. On a non-default stack this is the
        adapter's own route spec rather than the host's default gateway."""
        rs = getattr(getattr(vnic, "spec", None), "ipRouteSpec", None)
        gw = getattr(getattr(rs, "ipRouteConfig", None), "defaultGateway", None)
        return gw or None

    def ensure_vmk(self, fqdn, dvs, pg_name, ip, mask, mtu, stack, tag, apply_,
                   gateway=None):
        host = self.find(self.vim.HostSystem, fqdn)
        if host is None:
            return BLOCKED, "%s is not in this vCenter yet" % fqdn
        ns = host.configManager.networkSystem
        pg = next((p for p in dvs.portgroup if p.name == pg_name), None)
        for vnic in ns.networkInfo.vnic:
            if vnic.spec.ip and vnic.spec.ip.ipAddress == ip:
                have = self.vmk_gateway(vnic)
                if have != gateway:
                    # Reported, never changed. A gateway that differs from the rest of
                    # the cluster is worth seeing; silently rewriting one is not this
                    # step's business.
                    return OK, ("%s %s %s - gateway %s, config says %s"
                                % (fqdn, vnic.device, ip, have or "none",
                                   gateway or "none"))
                return OK, "%s %s %s" % (fqdn, vnic.device, ip)
        # An adapter already on this portgroup at another address. Adding a second one
        # would leave the host with two vmkernels on the same subnet.
        if pg is not None:
            for vnic in ns.networkInfo.vnic:
                dvp = getattr(vnic.spec, "distributedVirtualPort", None)
                if dvp is None or getattr(dvp, "portgroupKey", None) != pg.key:
                    continue
                have = getattr(getattr(vnic.spec, "ip", None), "ipAddress", None)
                if have and have != ip:
                    return BLOCKED, ("%s: %s is already on %s at %s, not %s. Adding a "
                                     "second adapter there would put two vmkernels on "
                                     "one subnet - correct the address or the range"
                                     % (fqdn, vnic.device, pg_name, have, ip))
        if not apply_:
            return ADD, "%s: vmkernel on %s at %s" % (fqdn, pg_name, ip)
        if pg is None:
            return BLOCKED, "%s: portgroup %s does not exist" % (fqdn, pg_name)
        spec = self.vim.host.VirtualNic.Specification(
            ip=self.vim.host.IpConfig(dhcp=False, ipAddress=ip, subnetMask=mask),
            mtu=mtu,
            distributedVirtualPort=self.vim.dvs.PortConnection(
                switchUuid=dvs.uuid, portgroupKey=pg.key))
        if stack:
            spec.netStackInstanceKey = stack
        if gateway:
            spec.ipRouteSpec = self.vim.host.VirtualNic.IpRouteSpec(
                ipRouteConfig=self.vim.host.IpRouteConfig(defaultGateway=gateway))
        device = ns.AddVirtualNic(portgroup="", nic=spec)
        if tag:
            host.configManager.virtualNicManager.SelectVnicForNicType(nicType=tag, device=device)
        return ADD, "%s: %s at %s" % (fqdn, device, ip)

    # ---- the management vmkernel
    def vds_by_name(self, dc_name, name):
        """The switch on its own, so a step can run without the vds step in the same pass."""
        dc = self.find(self.vim.Datacenter, dc_name)
        return self.find(self.vim.DistributedVirtualSwitch, name, dc.networkFolder) if dc else None

    def mgmt_source(self, host, device):
        """Where device sits now: (vnic, standard portgroup, vswitch, its pnics, its VLAN).

        Everything the migration has to read before it changes anything.
        """
        info = host.configManager.networkSystem.networkInfo
        vnic = next((v for v in info.vnic if v.device == device), None)
        if vnic is None:
            return None, None, None, [], None
        pg = next((p for p in info.portgroup
                   if p.spec.name == getattr(vnic, "portgroup", None)), None)
        if pg is None:
            return vnic, None, None, [], None
        sw = next((v for v in info.vswitch if v.name == pg.spec.vswitchName), None)
        pnics = [p.rsplit("-", 1)[-1] for p in (sw.pnic or [])] if sw else []
        return vnic, pg, sw, pnics, pg.spec.vlanId

    @staticmethod
    def dvpg_vlan(pg):
        """A distributed portgroup's access VLAN, or None when it is a trunk or private."""
        vlan = getattr(getattr(pg.config, "defaultPortConfig", None), "vlan", None)
        vid = getattr(vlan, "vlanId", None)
        return vid if isinstance(vid, int) else None

    @staticmethod
    def on_dvpg(vnic):
        """The distributed port a vmkernel is on, or None when it is still on a vSwitch."""
        return getattr(getattr(vnic, "spec", None), "distributedVirtualPort", None)

    def migrate_mgmt_vmk(self, fqdn, dvs, pg_name, device, allow_vlan_change, apply_,
                         evidence=None, want_pnics=()):
        """Move device and the uplink carrying it onto dvs in one host transaction.

        Done as two calls - the pnic, then the vmkernel - the host is unreachable between
        them and never comes back. One UpdateNetworkConfig carries both, so the host
        applies them together or not at all, and reverts by itself if the change costs it
        the vCenter. That rollback is silent, so this reads the result back rather than
        trusting the call to have stuck.
        """
        host = self.find(self.vim.HostSystem, fqdn)
        if host is None:
            return BLOCKED, "%s is not in this vCenter yet" % fqdn
        # Read where the vmkernel is before looking for anywhere to put it: when the
        # target portgroup is missing, the VLAN it has to be built on is the thing the
        # operator actually needs, and it is right here.
        vnic, src_pg, src_sw, pnics, src_vlan = self.mgmt_source(host, device)
        if vnic is None:
            return BLOCKED, "%s has no %s" % (fqdn, device)
        pg = next((p for p in dvs.portgroup if p.name == pg_name), None)

        # Already moved is not the same as finished. The uplink is a separate step, so
        # returning here on a re-run would leave a host on one nic for good.
        port = self.on_dvpg(vnic)
        moved = port is not None and pg is not None and port.portgroupKey == pg.key
        if port is not None and not moved:
            other = next((p.name for p in dvs.portgroup if p.key == port.portgroupKey),
                         port.portgroupKey)
            return BLOCKED, ("%s: %s is already on distributed portgroup %s, not %s - move it by "
                             "hand or point the config at %s" % (fqdn, device, other, pg_name, other))
        if moved:
            return self.add_uplink(host, dvs, fqdn, device, pg_name,
                                   list(pnics) + list(want_pnics), apply_, evidence)
        if pg is None:
            if src_pg is not None:
                return BLOCKED, ("%s: portgroup %s does not exist yet. %s is on %s VLAN %s today, "
                                 "so set network.portgroups.management.vlan to %s and run "
                                 "--only vds --commit to create it"
                                 % (fqdn, pg_name, device, src_pg.spec.name, src_vlan or 0,
                                    src_vlan or 0))
            return BLOCKED, ("%s: portgroup %s does not exist - run --only vds --commit first"
                             % (fqdn, pg_name))
        if src_pg is None or src_sw is None:
            return BLOCKED, "%s: cannot tell which standard switch %s is on" % (fqdn, device)
        if not pnics:
            return BLOCKED, ("%s: %s is on %s, which has no uplink - nothing to move"
                             % (fqdn, device, src_sw.name))
        if not any(m.config.host == host for m in dvs.config.host):
            return BLOCKED, "%s is not on %s yet - run --only vds first" % (fqdn, dvs.name)

        # the one check that matters: a vmkernel landing on a VLAN its physical port does
        # not carry is exactly how these migrations lose a host
        want = self.dvpg_vlan(pg)
        if want is None:
            return BLOCKED, ("%s: %s is not a plain access VLAN, so this cannot tell whether the "
                             "move is safe - migrate %s in the vSphere Client" % (fqdn, pg_name, device))
        if want != (src_vlan or 0) and not allow_vlan_change:
            return BLOCKED, ("%s: %s is on VLAN %s today, %s is VLAN %d. Moving it strands the host "
                             "unless the physical port carries both. Correct the VLAN, or set "
                             "network.management.allow_vlan_change: true to accept the change"
                             % (fqdn, device, src_vlan or 0, pg_name, want))
        if not apply_:
            return ADD, ("%s: move %s and %s off %s onto %s/%s (VLAN %d)"
                         % (fqdn, device, ",".join(pnics), src_sw.name, dvs.name, pg_name, want))

        v = self.vim
        ns = host.configManager.networkSystem

        # The server does not always fill in invalidProperty, so keep the exact request
        # and the whole fault. Neither holds a secret, and between them they say what
        # this host objected to without another attempt at it.
        def note(name, body):
            if not evidence:
                return
            try:
                with open(os.path.join(evidence, "mgmt-%s.%s.txt" % (fqdn, name)), "w") as fh:
                    fh.write(body)
            except OSError:
                pass

        # Step one: the vmkernel alone, onto an uplink the switch already has.
        #
        # Not both at once. Moving the uplink in the same breath needs a free uplink
        # port assigned by hand, and lands the vmkernel on a switch whose teaming may
        # put it on either nic - so it only works if both trunks carry the VLAN, which
        # is the very thing it would be betting on. Going vmkernel-first bets on one
        # nic that is demonstrably already carrying this host's vMotion and vSAN, and
        # if that bet is wrong ESXi reverts the change by itself within seconds.
        #
        # An edit replaces the spec outright, so start from the one the host reports:
        # a bare spec would ask it to keep vmk0 and discard its address.
        # A copy: mutating the live object would also change what the read-back below
        # sees, and the rollback check would always say the move stuck.
        spec = copy.deepcopy(vnic.spec)
        spec.portgroup = None
        spec.distributedVirtualPort = v.dvs.PortConnection(
            switchUuid=dvs.uuid, portgroupKey=pg.key)
        nc = v.host.NetworkConfig()
        nc.vnic = [v.host.VirtualNic.Config(
            changeOperation="edit", device=device, portgroup="", spec=spec)]
        note("request", str(nc))
        try:
            ns.UpdateNetworkConfig(nc, "modify")
        except Exception as e:
            note("fault", "%s\n\n%s" % (type(e).__name__, getattr(e, "error", None) or e))
            raise

        landed = self.on_dvpg(self.mgmt_source(host, device)[0])
        if landed is None or landed.portgroupKey != pg.key:
            return BLOCKED, ("%s: %s did not stay on %s - the host rolled the change back, so "
                             "VLAN %d does not reach it over the uplinks %s already has. "
                             "Nothing was changed." % (fqdn, device, pg_name, want, dvs.name))

        state, detail = self.add_uplink(host, dvs, fqdn, device, pg_name,
                                        list(pnics) + list(want_pnics), apply_, evidence)
        # the vmkernel did move just now, so this run changed something either way
        if state == OK:
            return ADD, "%s: %s now on %s/%s" % (fqdn, device, dvs.name, pg_name)
        return state, detail

    def free_uplinks(self, dvs, host, want):
        """`want` free uplink ports on this host's side of the switch, keys and all.

        A nic offered with no uplink port is accepted when a host first joins the
        switch - vCenter picks one - but not when editing a host that is already a
        member. The existing nic's spec carries uplinkPortKey and uplinkPortgroupKey,
        so the new one needs the same, naming a port nothing is connected to.
        """
        v = self.vim
        try:
            ports = dvs.FetchDVPorts(v.dvs.PortCriteria(uplinkPort=True, host=[host]))
        except Exception:
            return []
        out = []
        for port in ports or []:
            if getattr(port, "connectee", None) is None:
                out.append((port.key, getattr(port, "portgroupKey", None)))
            if len(out) >= want:
                break
        return out

    def uplink_specs(self, existing, move, slots):
        """Every nic the host should uplink: the ones it has, plus the ones being added.

        Rebuilt rather than echoed. connectionCookie is the server's own handle on a
        live port connection, and handing it back in a reconfigure is not something the
        client does; the device and its uplink port are what identify the nic.
        """
        v = self.vim
        out = []
        for s in existing:
            ps = v.dvs.HostMember.PnicSpec(pnicDevice=s.pnicDevice)
            for k in ("uplinkPortKey", "uplinkPortgroupKey"):
                if getattr(s, k, None):
                    setattr(ps, k, getattr(s, k))
            out.append(ps)
        for q, (port_key, pg_key) in zip(move, slots):
            ps = self.vim.dvs.HostMember.PnicSpec(pnicDevice=q, uplinkPortKey=port_key)
            if pg_key:
                ps.uplinkPortgroupKey = pg_key
            out.append(ps)
        return out

    def release_pnics(self, info, move):
        """Standard-switch edits that let go of the nics being moved.

        A nic belongs to one switch at a time. Asking the distributed switch to take
        one that a vSwitch still holds gets "An error occurred during host
        configuration" and no more, so the release has to travel with the claim.
        """
        v = self.vim
        out = []
        for sw in (info.vswitch or []):
            owned = [q.rsplit("-", 1)[-1] for q in (sw.pnic or [])]
            drop = [q for q in move if q in owned]
            if not drop:
                continue
            keep = [q for q in owned if q not in drop]
            spec = copy.deepcopy(sw.spec)
            bridge = getattr(spec, "bridge", None)
            if keep and bridge is not None and hasattr(bridge, "nicDevice"):
                bridge.nicDevice = keep            # keep beacon and LLDP as they were
            else:
                spec.bridge = None                 # nothing left to uplink with
            # The teaming policy names its uplinks too, and a policy that still lists a
            # nic the switch no longer has is refused.
            order = getattr(getattr(spec, "nicTeaming", None) or
                            getattr(getattr(spec, "policy", None), "nicTeaming", None),
                            "nicOrder", None)
            if order is not None:
                for field in ("activeNic", "standbyNic"):
                    listed = getattr(order, field, None)
                    if listed:
                        setattr(order, field, [q for q in listed if q not in drop])
            out.append(v.host.VirtualSwitch.Config(
                changeOperation="edit", name=sw.name, spec=spec))
        return out

    def add_uplink(self, host, dvs, fqdn, device, pg_name, pnics, apply_, evidence=None):
        """Step two, on its own so a re-run can finish what a first run started.

        By the same call that put this host on the switch in the first place. Adding a
        nic cannot cut off a vmkernel that is already up on another one, and
        ReconfigureDvs assigns the uplink port itself.
        """
        v = self.vim
        info = host.configManager.networkSystem.networkInfo
        proxy = next((p for p in (info.proxySwitch or []) if p.dvsUuid == dvs.uuid), None)
        existing = list(getattr(getattr(getattr(proxy, "spec", None), "backing", None),
                                "pnicSpec", None) or [])
        have = {s.pnicDevice for s in existing}
        move = []
        for q in pnics:                       # de-duplicated, order kept
            if q not in have and q not in move:
                move.append(q)
        if not move:
            return OK, "%s: %s on %s/%s, %s uplinked" % (fqdn, device, dvs.name, pg_name,
                                                         ",".join(sorted(have)) or "no nic")
        if not apply_:
            return ADD, "%s: add %s to %s as an uplink" % (fqdn, ",".join(move), dvs.name)
        slots = self.free_uplinks(dvs, host, len(move))
        if len(slots) < len(move):
            return CHANGE, ("%s: %s is on %s/%s, but %s has only %d free uplink port(s) "
                            "for %d nic(s). The host is up and reachable; it is one nic "
                            "short of redundancy"
                            % (fqdn, device, dvs.name, pg_name, dvs.name, len(slots),
                               len(move)))
        # Release and claim in one host transaction, the way the wizard does it.
        nc = v.host.NetworkConfig()
        nc.vswitch = self.release_pnics(info, move)
        nc.proxySwitch = [v.host.HostProxySwitch.Config(
            changeOperation="edit", uuid=dvs.uuid,
            spec=v.host.HostProxySwitch.Specification(
                backing=v.dvs.HostMember.PnicBacking(
                    pnicSpec=self.uplink_specs(existing, move, slots))))]
        try:
            if evidence:
                try:
                    with open(os.path.join(evidence,
                                           "mgmt-%s.uplink-request.txt" % fqdn), "w") as fh:
                        fh.write(str(nc))
                except OSError:
                    pass
            host.configManager.networkSystem.UpdateNetworkConfig(nc, "modify")
        except Exception as e:
            if evidence:
                try:
                    with open(os.path.join(evidence, "mgmt-%s.uplink-fault.txt" % fqdn), "w") as fh:
                        fh.write("%s\n\n%s" % (type(e).__name__, getattr(e, "error", None) or e))
                except OSError:
                    pass
            return CHANGE, ("%s: %s is on %s/%s, but %s could not be added as an uplink (%s). "
                            "The host is up and reachable; it is one nic short of redundancy"
                            % (fqdn, device, dvs.name, pg_name, ",".join(move), fault(e)))
        # read it back: the host reverts a network change that costs it the vCenter
        after = host.configManager.networkSystem.networkInfo
        now = next((q for q in (after.proxySwitch or []) if q.dvsUuid == dvs.uuid), None)
        landed = {s.pnicDevice for s in (getattr(getattr(getattr(now, "spec", None),
                                                         "backing", None),
                                                 "pnicSpec", None) or [])}
        missing = [q for q in move if q not in landed]
        if missing:
            return CHANGE, ("%s: %s is on %s/%s, but %s did not stay on the switch - the host "
                            "put it back. The host is up and reachable; it is one nic short "
                            "of redundancy"
                            % (fqdn, device, dvs.name, pg_name, ",".join(missing)))
        return ADD, ("%s: %s on %s/%s, %s added as an uplink"
                     % (fqdn, device, dvs.name, pg_name, ",".join(move)))

    # ---- vSphere Lifecycle Manager
    IMAGE_PATH = "/esx/settings/clusters/%s/enablement/software"

    def image_enabled(self, moid):
        """Whether the cluster is image-managed, asked of the service that owns it.

        ComputeResource.lifecycleManaged is the SOAP read of the same flag and is
        used as the fallback: it is right in practice but it is a property of the
        inventory object, not of Image Manager.
        """
        try:
            info = self.vapi().call("GET", self.IMAGE_PATH % moid) or {}
        except Exception as e:
            return None, str(e)
        return bool(info.get("enabled")), ""

    def image_check(self, moid):
        """The documented pre-flight, which is read-only. Returns (ok, lines)."""
        try:
            # checks_to_skip is required, not optional: an empty spec is a 400. An
            # empty list is the documented "run every check".
            res = self.vapi().task("POST", (self.IMAGE_PATH % moid) + "?action=check",
                                   {"checks_to_skip": []}, minutes=20)
        except Exception as e:
            return None, str(e)
        return True, vapi_message(res)

    def image_seed_version(self, cl):
        """The ESXi build the hosts already run, as a vLCM base-image version.

        Setting the desired image to what is already installed is the whole point:
        every host lands compliant, so the conversion implies no remediation.
        """
        seen = []
        for h in (cl.host or []):
            prod = getattr(getattr(h, "config", None), "product", None)
            ver, build = getattr(prod, "version", None), getattr(prod, "build", None)
            if ver and build and (ver, build) not in seen:
                seen.append((ver, build))
        return seen

    def ensure_cluster_image(self, cluster_name, apply_, evidence=None):
        """A cluster managed by a vLCM image rather than by baselines.

        The VCF import refuses a baseline-managed cluster outright. This is the
        documented Image Manager enablement API - check, then enable, then verify -
        rather than the vSphere Client walk-through, because we repeat this at every
        site. It is one-way, so it only runs under --commit.
        """
        cl = self.find(self.vim.ClusterComputeResource, cluster_name)
        if cl is None:
            return BLOCKED, "cluster %s does not exist yet" % cluster_name
        moid = getattr(cl, "_moId", None) or str(cl)
        enabled, why = self.image_enabled(moid)
        if enabled is None:                     # REST unreachable; trust the inventory
            enabled = bool(getattr(cl, "lifecycleManaged", False))
        if enabled:
            return OK, "%s is managed by a vLCM image" % cluster_name

        hosts = list(cl.host or [])
        if not hosts:
            return BLOCKED, "%s has no hosts yet" % cluster_name
        down = [h.name for h in hosts
                if getattr(getattr(h, "runtime", None), "connectionState", "") != "connected"]
        old = [h.name for h in hosts
               if not version_at_least(
                   getattr(getattr(getattr(h, "config", None), "product", None),
                           "version", "0"), "7.0")]
        if down:
            return BLOCKED, ("%s cannot convert to an image while %s %s disconnected - every "
                             "host has to be eligible" % (cluster_name, ", ".join(down),
                                                          "is" if len(down) == 1 else "are"))
        if old:
            return BLOCKED, ("%s cannot convert to an image: %s %s below ESXi 7.0"
                             % (cluster_name, ", ".join(old), "is" if len(old) == 1 else "are"))

        builds = self.image_seed_version(cl)
        running = ", ".join("%s build %s" % (v, b) for v, b in builds) or "an unknown build"
        if not apply_:
            return ADD, ("%s: convert from baselines to a vLCM image, over the Image Manager "
                         "enablement API, on %d eligible hosts running %s. One way - a cluster "
                         "on an image cannot go back to baselines - so this needs --commit"
                         % (cluster_name, len(hosts), running))

        ok, lines = self.image_check(moid)
        if evidence and lines:
            try:
                with open(os.path.join(evidence, "image-%s.check.txt" % cluster_name), "w") as fh:
                    fh.write(lines)
            except OSError:
                pass
        if ok is None:
            return BLOCKED, self.image_by_hand(cluster_name, hosts,
                                               "the enablement check could not run (%s)" % lines)
        try:
            self.vapi().task("PUT", self.IMAGE_PATH % moid,
                             {"skip_software_check": False}, minutes=60)
        except Exception as e:
            return BLOCKED, self.image_by_hand(cluster_name, hosts, fault(e))

        # Read it back from the service, not from our own request succeeding.
        enabled, why = self.image_enabled(moid)
        if enabled is None:
            enabled = bool(getattr(cl, "lifecycleManaged", False))
        if not enabled:
            return BLOCKED, self.image_by_hand(
                cluster_name, hosts, "the enable task returned but the cluster still reports "
                                     "baseline management")
        return ADD, ("%s is now managed by a vLCM image, seeded from what the hosts already "
                     "run (%s), so nothing is non-compliant. Do not remediate"
                     % (cluster_name, running))

    # What a refusal actually means, and what to do about it. An eligibility
    # condition is a property of the cluster, so the vSphere Client refuses for the
    # same reason - pointing at Setup Image there would send someone round a loop.
    IMAGE_REMEDIES = (
        ("transport node profile", True,
         "NSX is installed on these hosts but no Transport Node Profile is attached to the "
         "cluster, and vLCM will not manage a cluster whose NSX hosts were prepared one by "
         "one. VCF attaches a profile when it prepares a cluster and then usually detaches "
         "it, so one probably already exists. In NSX Manager: System > Fabric > Compute "
         "Managers, check the vCenter is Up; then System > Fabric > Hosts, set Managed by to "
         "that vCenter, select %(cluster)s, Configure NSX, and apply the profile that matches "
         "what the hosts already run. Wait for Success, then run this step again."),
        ("compute manager", True,
         "NSX cannot reach the vCenter, so the eligibility check cannot read the hosts' NSX "
         "state. In NSX Manager: System > Fabric > Compute Managers - the connection has to "
         "be Up. Then run this step again."),
        ("stateless", True,
         "vLCM images do not manage stateless hosts. %(cluster)s has to be stateful before "
         "it can convert, and that is a rebuild, not a setting."),
        ("remediation", True,
         "a host is mid-remediation through Update Manager. Let it finish, then run this "
         "step again."),
        ("software specification", False, None),   # the one case Setup Image is the answer
    )

    def image_by_hand(self, cluster_name, hosts, because):
        """What to do about a refusal, which is not always 'do it in the client'."""
        low = because.lower()
        for needle, own_remedy, text in self.IMAGE_REMEDIES:
            if needle in low and own_remedy:
                return "%s cannot convert to a vLCM image yet: %s" % (
                    cluster_name, text % {"cluster": cluster_name})
        return ("%s is baseline-managed and the import refuses that; %s. All %d hosts are "
                "eligible, so convert it in the vSphere Client: %s > Updates > Image > Setup "
                "Image, importing the image from %s so the desired image is exactly what the "
                "hosts already run. Do not remediate afterwards. One way: a cluster on an "
                "image cannot go back to baselines."
                % (cluster_name, because, len(hosts), cluster_name, hosts[0].name))

    # ---- vSAN
    def ha_enabled(self, cl):
        das = getattr(cl.configurationEx, "dasConfig", None)
        return bool(das is not None and das.enabled)

    def set_ha(self, cl, on):
        self.wait(cl.ReconfigureComputeResource_Task(self.cluster_spec(on, self.drs_on(cl)), modify=True))

    def drs_on(self, cl):
        drs = getattr(cl.configurationEx, "drsConfig", None)
        return bool(drs is not None and drs.enabled)

    def ntp_state(self, host):
        """What the host reports about time: servers, and the daemon behind them.

        Three things have to line up and VCF checks all three. The servers are only
        half of it: a configured server with a stopped daemon is a host whose clock
        drifts, and a running daemon behind a closed ntpClient ruleset is a host that
        never reaches the server at all. That last one is the quiet failure - the
        service reads as running, so a glance at the UI says the host is fine.
        """
        dts = getattr(host.configManager, "dateTimeSystem", None)
        info = getattr(dts, "dateTimeInfo", None) if dts is not None else None
        ntp = getattr(info, "ntpConfig", None)
        servers = [str(s) for s in (getattr(ntp, "server", None) or [])]
        svc = None
        ss = getattr(host.configManager, "serviceSystem", None)
        for s in (getattr(getattr(ss, "serviceInfo", None), "service", None) or []):
            if getattr(s, "key", None) == "ntpd":
                svc = s
                break
        fw = getattr(host.configManager, "firewallSystem", None)
        allowed = None                      # None = could not read it, not "closed"
        for rs in (getattr(getattr(fw, "firewallInfo", None), "ruleset", None) or []):
            if getattr(rs, "key", None) == "ntpClient":
                allowed = bool(getattr(rs, "enabled", False))
                break
        return {"servers": servers,
                "running": bool(getattr(svc, "running", False)) if svc else False,
                "policy": str(getattr(svc, "policy", "")) if svc else "",
                "present": svc is not None,
                "firewall": allowed}

    def ensure_ntp(self, fqdn, servers, apply_):
        """NTP configured, the daemon running, and set to start with the host.

        policy 'on' is the UI's "Start and stop with host". VCF wants that, not
        'automatic' - automatic ties the daemon to the firewall ruleset, so a host
        that comes up with the ruleset closed leaves ntpd stopped and the import
        fails on a host that looked correct the last time anyone looked at it.
        """
        host = self.find(self.vim.HostSystem, fqdn)
        if host is None:
            return [(BLOCKED, "%s is not in this vCenter yet" % fqdn)]
        want = [str(s) for s in servers]
        state = self.ntp_state(host)
        if not state["present"]:
            return [(BLOCKED, "%s has no ntpd service - this is not an ESXi host the "
                              "tool knows how to configure" % fqdn)]

        rows, need_restart = [], False
        if state["servers"] != want:
            if not apply_:
                rows.append((CHANGE, "%s: NTP servers %s -> %s" % (
                    fqdn, ", ".join(state["servers"]) or "none", ", ".join(want))))
            else:
                host.configManager.dateTimeSystem.UpdateDateTimeConfig(
                    self.vim.host.DateTimeConfig(
                        ntpConfig=self.vim.host.NtpConfig(server=want)))
                rows.append((CHANGE, "%s: NTP servers set to %s" % (fqdn, ", ".join(want))))
                need_restart = True
        else:
            rows.append((OK, "%s: NTP %s" % (fqdn, ", ".join(want))))

        # The ruleset before the daemon: starting ntpd through a closed one gets a
        # service that runs and syncs nothing.
        if state["firewall"] is False:
            if not apply_:
                rows.append((CHANGE, "%s: open the ntpClient firewall ruleset" % fqdn))
            else:
                host.configManager.firewallSystem.EnableRuleset(id="ntpClient")
                rows.append((CHANGE, "%s: ntpClient firewall ruleset opened" % fqdn))

        ss = host.configManager.serviceSystem
        if state["policy"] != "on":
            if not apply_:
                rows.append((CHANGE, "%s: ntpd start policy %s -> on (start and stop "
                                     "with host)" % (fqdn, state["policy"] or "unset")))
            else:
                ss.UpdateServicePolicy(id="ntpd", policy="on")
                rows.append((CHANGE, "%s: ntpd set to start with the host" % fqdn))

        if not state["running"]:
            if not apply_:
                rows.append((CHANGE, "%s: start ntpd" % fqdn))
            else:
                ss.StartService(id="ntpd")
                rows.append((CHANGE, "%s: ntpd started" % fqdn))
        elif need_restart:
            ss.RestartService(id="ntpd")
            rows.append((CHANGE, "%s: ntpd restarted to pick up the new servers" % fqdn))
        elif state["servers"] == want and state["policy"] == "on":
            rows.append((OK, "%s: ntpd running, starts with the host" % fqdn))

        if apply_:
            # Read it back. A policy or start that did not take is worth knowing now,
            # not at the import.
            after = self.ntp_state(host)
            if after["servers"] != want or not after["running"] or after["policy"] != "on":
                rows.append((BLOCKED, "%s: after configuring, the host still reports "
                                      "servers=%s running=%s policy=%s"
                             % (fqdn, ", ".join(after["servers"]) or "none",
                                after["running"], after["policy"] or "unset")))
        return rows

    def ensure_vsan(self, cluster_name, want_ha, groups, apply_):
        """vSphere refuses to configure vSAN while HA is on, so HA goes on last - after
        vSAN is enabled and has capacity. Re-run once the disk groups are claimed."""
        cl = self.find(self.vim.ClusterComputeResource, cluster_name)
        if cl is None:
            return [(BLOCKED, "cluster %s does not exist yet" % cluster_name)]
        rows, on = [], self.ha_enabled(cl)
        cfg = getattr(cl.configurationEx, "vsanConfigInfo", None)
        if cfg is None or not cfg.enabled:
            if not apply_:
                return [(ADD, "turn vSAN on for %s%s" % (
                    cluster_name, " (HA off first, back on once disks are claimed)" if on else ""))]
            if on:
                self.set_ha(cl, False)
                rows.append((CHANGE, "HA off: vSAN cannot be configured while it is on"))
            spec = self.vim.cluster.ConfigSpecEx(
                vsanConfig=self.vim.vsan.cluster.ConfigInfo(
                    enabled=True,
                    defaultConfig=self.vim.vsan.cluster.ConfigInfo.HostDefaultInfo(
                        autoClaimStorage=False)))
            self.wait(cl.ReconfigureComputeResource_Task(spec, modify=True))
            rows.append((ADD, "vSAN turned on for %s" % cluster_name))
            on = False
        else:
            rows.append((OK, "vSAN on for %s" % cluster_name))

        rows += self.ensure_disk_groups(cl, groups, apply_)

        if not want_ha or on:
            return rows
        # HA only once the datastore has capacity: on an empty vSAN it has nowhere to
        # place its heartbeats and the cluster comes up with a configuration alarm.
        if not self.vsan_capacity(cl):
            rows.append((SKIP, "HA stays off until the disk groups are claimed - re-run then"))
            return rows
        if not apply_:
            rows.append((ADD, "turn HA back on for %s" % cluster_name))
            return rows
        try:
            self.set_ha(cl, True)
            rows.append((ADD, "HA back on for %s" % cluster_name))
        except Exception as e:
            rows.append((BLOCKED, "could not turn HA back on: %s" % fault(e)))
        return rows

    @staticmethod
    def _size(disk):
        c = disk.capacity
        return c.block * c.blockSize

    @staticmethod
    def _model(disk):
        return (getattr(disk, "model", "") or disk.canonicalName).strip()

    @staticmethod
    def _gb(n):
        return "%.0f GB" % (n / 1000000000.0)

    def disk_group_plan(self, free, groups="auto"):
        """One cache disk per group, the capacity disks dealt round robin between them.

        Left on auto the hardware decides: the smallest size class is the cache tier, so
        one 1.92 TB beside five 3.84 TB gives one group, and two beside four gives two.
        That is what the vSphere Client proposes, and it never drafts a capacity drive
        into cache. All one size, there is nothing to infer, so one group. A number
        overrides it, capped at what the disks allow."""
        ds = sorted(free, key=self._size)
        if len(ds) < 2:
            return []
        if groups in (None, "", "auto", "Auto", "AUTO"):
            smallest = self._size(ds[0])
            n = sum(1 for d in ds if self._size(d) == smallest)
            if n == len(ds):                           # every disk the same size
                n = 1
        else:
            n = int(groups)
        n = max(1, min(n, len(ds) // 2))
        out = [(c, []) for c in ds[:n]]
        for i, d in enumerate(ds[n:]):
            out[i % n][1].append(d)
        return [(c, caps) for c, caps in out if caps]

    def ensure_disk_groups(self, cl, groups, apply_):
        """Claim the disks vSAN reports as eligible. Anything in use, ineligible or
        carrying a partition is never touched - QueryDisksForVsan has already ruled
        those out, and a host that is partly claimed is left for a person."""
        rows = []
        for h in sorted(getattr(cl, "host", []), key=lambda x: x.name):
            try:
                disks = h.configManager.vsanSystem.QueryDisksForVsan()
            except Exception as e:
                rows.append((BLOCKED, "%s: cannot read its disks (%s)" % (h.name, fault(e))))
                continue
            inuse = [d.disk for d in disks if d.state == "inUse"]
            free = [d.disk for d in disks if d.state == "eligible" and not d.error]
            if inuse:
                rows.append((OK, "%s: %d disks already claimed%s" % (
                    h.name, len(inuse), ", %d left alone" % len(free) if free else "")))
                continue
            if len(free) < 2:
                # Say which disks were refused and why, rather than only the count.
                # Almost always a stale partition: esx_ready --wipe-disks clears it.
                why = {}
                for d in disks:
                    if d.state == "eligible" and not d.error:
                        continue
                    reason = fault(d.error) if d.error else (d.state or "not eligible")
                    why.setdefault(reason, []).append(d.disk.canonicalName)
                detail = "; ".join("%d x %s" % (len(v), k) for k, v in sorted(why.items()))
                rows.append((SKIP, "%s: %d of %d disks eligible, needs 2 for a group%s" % (
                    h.name, len(free), len(disks), " - " + detail if detail else "")))
                continue
            plan = self.disk_group_plan(free, groups)
            flash = all(getattr(d, "ssd", False) for d in free)
            shape = "; ".join("cache %s %s + %d x %s %s" % (
                self._model(c), self._gb(self._size(c)),
                len(caps), self._model(caps[0]), self._gb(self._size(caps[0])))
                for c, caps in plan)
            if not apply_:
                rows.append((ADD, "%s: claim %d disks as %d %s group(s) - %s" % (
                    h.name, len(free), len(plan), "all-flash" if flash else "hybrid", shape)))
                continue
            try:
                self.claim_disks(h, plan, flash)
            except Exception as e:
                rows.append((BLOCKED, "%s: claiming disks failed (%s)" % (h.name, fault(e))))
                continue
            rows.append((ADD, "%s: claimed %d disks as %d %s group(s)" % (
                h.name, len(free), len(plan), "all-flash" if flash else "hybrid")))
        return rows

    def claim_disks(self, host, plan, flash):
        """All-flash claiming does not go through HostVsanSystem: its DiskMapping has a
        nonSsd field, and an SSD put there is refused ("is an SSD and can not be used as
        a vSAN HDD"). The vSAN management endpoint takes cache and capacity separately
        and is told which kind of cluster it is building."""
        import vsanapiutils
        spec = self.vim.vsan.host.DiskMappingCreationSpec(
            host=host,
            cacheDisks=[c for c, _ in plan],
            capacityDisks=[d for _, caps in plan for d in caps],
            creationType="allFlash" if flash else "hybrid")
        task = self.vsan_disk_manager().InitializeDiskMappings(spec)
        self.wait(vsanapiutils.ConvertVsanTaskToVcTask(task, self.si._stub))

    def vsan_disk_manager(self):
        if getattr(self, "_dms", None) is None:
            import vsanapiutils
            self._dms = vsanapiutils.GetVsanVcMos(
                self.si._stub, context=ssl._create_unverified_context()
            )["vsan-disk-management-system"]
        return self._dms

    def vsan_capacity(self, cl):
        """True once some host in the cluster has a claimed vSAN disk."""
        for h in getattr(cl, "host", []):
            try:
                dm = h.configManager.vsanSystem.QueryDisksForVsan()
            except Exception:
                continue
            if any(getattr(d, "state", "") == "inUse" for d in dm or []):
                return True
        return False


def vcenter_ssh(fqdn, user, password, apply_):
    """The import needs SSH on the appliance. Appliance REST API, no extra dependency."""
    base = "https://%s/api" % fqdn
    ctx = ssl._create_unverified_context()

    def call(method, path, body=None, token=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("vmware-api-session-id", token)
        else:
            import base64
            cred = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
            req.add_header("Authorization", "Basic " + cred)
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else None

    try:
        token = call("POST", "/session")
        state = call("GET", "/appliance/access/ssh", token=token)
    except Exception as e:
        return BLOCKED, "cannot read the SSH setting on %s (%s)" % (fqdn, fault(e))
    if state is True or state == {"enabled": True}:
        return OK, "SSH enabled on %s" % fqdn
    if not apply_:
        return ADD, "enable SSH on %s" % fqdn
    try:
        call("PUT", "/appliance/access/ssh", {"enabled": True}, token=token)
    except Exception as e:
        return BLOCKED, "could not enable SSH on %s (%s)" % (fqdn, fault(e))
    return ADD, "enabled SSH on %s" % fqdn


# ---------------------------------------------------------------- steps
def step_dns(cfg, st, apply_):
    r = Result("dns")
    ok, detail = dns_check(need(cfg, "vcenter.fqdn", "the appliance FQDN"), opt(cfg, "vcenter.ip"))
    r.add(OK if ok else BLOCKED, detail)
    for h in st["hosts"]:
        ok, detail = dns_check(h)
        r.add(OK if ok else BLOCKED, detail)
    return r


def step_esx(cfg, st, apply_):
    r = Result("esx")
    want = str(opt(cfg, "esx.build", "")) or None
    for h in st["hosts"]:
        version, build = soap_about(h)
        if not version:
            r.add(BLOCKED, "%s does not answer on 443" % h)
        elif want and build != want:
            r.add(BLOCKED, "%s is on %s build %s, config wants %s" % (h, version, build, want))
        else:
            r.add(OK, "%s ESX %s build %s" % (h, version, build))
    return r


def step_vcsa(cfg, st, apply_):
    r = Result("vcsa")
    fqdn = need(cfg, "vcenter.fqdn", "the appliance FQDN")
    version, build = soap_about(fqdn, timeout=5)
    if version:
        st["vc_up"] = True
        return r.add(OK, "%s already answering, vCenter %s build %s" % (fqdn, version, build))
    spec_path = os.path.join(st["outdir"], "vcsa-deploy.json")
    precheck = st.get("precheck")
    vendor = st.get("vendor_precheck")
    if vendor and not apply_:
        return r.add(BLOCKED, "--vendor-precheck runs the installer and needs --commit")
    if not apply_ and not precheck:
        return r.add(ADD, "deploy %s (writes %s)" % (fqdn, os.path.basename(spec_path)))
    spec = vcsa_spec(cfg, st["pw"].get("vcenter.target.password"),
                     st["pw"].get("vcenter.root_password"),
                     st["pw"].get("vcenter.vc_password"))
    if precheck and not apply_:
        # --precheck never writes the spec to disk and never runs the installer
        tgt = cfg["vcenter"]["target"]
        for problem in spec_problems(spec, {
                "new_vcsa.esxi.deployment_network": tgt.get("network"),
                "new_vcsa.appliance.name": fqdn.split(".")[0],
                "new_vcsa.network.ip": opt(cfg, "vcenter.ip")}):
            r.add(BLOCKED, problem)
        free = ip_free(opt(cfg, "vcenter.ip"))
        r.add(OK if free else (SKIP if free is None else BLOCKED),
              "%s is free" % opt(cfg, "vcenter.ip") if free else
              ("cannot tell whether %s is free - no ping here" % opt(cfg, "vcenter.ip")
               if free is None else
               "%s already answers to ping - something else holds it" % opt(cfg, "vcenter.ip")))
        ok, detail = dns_check(fqdn, opt(cfg, "vcenter.ip"))
        r.add(OK if ok else BLOCKED, detail)
        if r.state != BLOCKED:
            r.add(CHANGE, "nothing was changed. --vendor-precheck --commit for the "
                          "installer's own checks, or --commit to deploy %s" % fqdn)
        return r
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=4)
    os.chmod(spec_path, 0o600)
    r.add(ADD, "wrote %s" % spec_path)
    installer = opt(cfg, "vcenter.installer")
    if not installer:
        return r.add(BLOCKED, "vcenter.installer is not set - run vcsa-deploy against that file by hand,"
                              " then delete it: it holds all three passwords in clear")
    base = [installer, "install", "--accept-eula", "--no-ssl-certificate-verification"]
    try:
        if vendor:
            # vcsa-deploy's own validation: target host, datastore, portgroup, address,
            # sizing and the password policy, without deploying anything.
            print("  running %s --precheck-only" % os.path.basename(installer))
            rc = subprocess.call(base + ["--precheck-only", spec_path])
            if rc != 0:
                return r.add(BLOCKED, "precheck exited %d - fix what it named above, then run it again" % rc)
            return r.add(OK, "precheck passed - %s is ready to deploy" % fqdn)
        cmd = base + [spec_path]
        print("  running %s" % " ".join(cmd[:2]))
        rc = subprocess.call(cmd)
        if rc != 0:
            return r.add(BLOCKED, "vcsa-deploy exited %d - read its log, then apply again" % rc)
        return r.add(ADD, "deployed %s" % fqdn)
    finally:
        # The spec carries three passwords in clear. It has done its job either way,
        # and a re-run writes a fresh one - so it never outlives the command.
        try:
            os.remove(spec_path)
            r.rows = [(s, d.replace("wrote ", "wrote and removed ")) for s, d in r.rows]
        except OSError:
            pass


def version_at_least(have, want):
    """Numeric compare, so 8.0.3 is below 9.1 and 9.1.1 is at or past 9.1."""
    def parts(v):
        out, run = [], ""
        for ch in str(v) + " ":
            if ch.isdigit():
                run += ch
            elif run:
                out.append(int(run))
                run = ""
        return out
    h, w = parts(have), parts(want)
    n = max(len(h), len(w))
    return h + [0] * (n - len(h)) >= w + [0] * (n - len(w))


def upgrade_template(installer):
    """The ISO ships upgrade templates. Using one keeps __version and the shape right
    for whatever release is mounted, rather than guessing at them here."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(installer)))
    for sub in ("templates/upgrade", "upgrade"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for dirpath, _, names in os.walk(d):
            for n in sorted(names):
                if n.endswith(".json") and "esxi" in n.lower():
                    try:
                        with open(os.path.join(dirpath, n)) as f:
                            return json.load(f), os.path.join(dirpath, n)
                    except (OSError, ValueError):
                        continue
    return None, None


def take_host_lock(host, outroot, site):
    """An exclusive lock per deployment target, held for as long as the installer runs.

    Two migrations deploying onto the same host at once is not a supported thing to do:
    they contend for the host and the datastore, and one of them auto-cancels and
    reverts after half an hour. The tool runs one site per process, so nothing inside
    a single run stops someone starting a second in another terminal. This does.

    Returns (handle, None) when taken, (None, who holds it) when not, (None, None)
    where locking is not available.
    """
    try:
        import fcntl
    except ImportError:
        return None, None
    safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in str(host))
    try:
        os.makedirs(outroot, exist_ok=True)
        fh = open(os.path.join(outroot, ".deploy-%s.lock" % safe), "a+")
    except OSError:
        return None, None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        who = fh.read().strip() or "another run"
        fh.close()
        return None, who
    import time as _t
    fh.seek(0)
    fh.truncate()
    fh.write("pid %d, site %s, since %s" % (os.getpid(), site, _t.strftime("%H:%M:%S")))
    fh.flush()
    return fh, None


def ip_free(ip, timeout=2):
    """Whether an address is unused, by the only read-only means there is.

    True free, False answered, None could not tell.
    """
    try:
        rc = subprocess.call(["ping", "-c", "1", "-W", str(int(timeout)), str(ip)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return rc != 0


def spec_problems(spec, expect):
    """What is wrong with the spec before anyone spends an hour finding out.

    Two kinds. The values this tool meant to set but did not, because following the
    template's shape means skipping keys the template does not declare - `expect` is
    what the caller believes it set. And leftovers - an empty password, a template
    placeholder - that pass JSON validation and then fail deep inside the installer.
    """
    bad = []
    for path, want in sorted(expect.items()):
        got = spec
        for part in path.split("."):
            got = got.get(part) if isinstance(got, dict) else None
        if got != want:
            bad.append("%s is %r, should be %r" % (path, got, want))

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k) if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
        elif isinstance(node, str):
            if node.startswith("<") and node.endswith(">"):
                bad.append("%s is still the template's %s" % (path, node))
            elif path.endswith("password") and not node:
                bad.append("%s is empty" % path)
    walk(spec, "")
    return bad


def read_appliance(host, root_pw, vm_ip, fqdn):
    """What the ESX host beneath the appliance can tell us, in one login.

    Two things the upgrade must not guess at. The portgroup, because the replacement
    has to land on the same network as the appliance it replaces and a configured name
    is a typo waiting to happen. And every VM name on the host, because during a
    migration both appliances exist at once, so the new one cannot be called what the
    old one is already called. The host already answers to these credentials - the
    upgrade spec names it as the source - so nothing extra is needed to go and look.

    Returns (portgroup, {names on the host}, None) or (None, set(), why not).
    """
    try:
        vs = VSphere(host, "root", root_pw)
    except Exception as e:
        return None, set(), "cannot log in to %s (%s)" % (host, fault(e))
    short = fqdn.split(".")[0]
    vms = list(vs._all(vs.vim.VirtualMachine))
    taken = {vm.name for vm in vms}
    for vm in vms:
        guest = getattr(vm, "guest", None)
        addrs = set()
        if guest is not None:
            if getattr(guest, "ipAddress", None):
                addrs.add(guest.ipAddress)
            for nic in (getattr(guest, "net", None) or []):
                for a in (getattr(nic, "ipAddress", None) or []):
                    addrs.add(a)
        names = {vm.name, getattr(guest, "hostName", None) or ""}
        if vm_ip not in addrs and fqdn not in names and short not in names:
            continue
        nets = [n.name for n in (getattr(vm, "network", None) or [])]
        if not nets:
            return None, taken, "%s on %s has no network attached" % (vm.name, host)
        if len(nets) > 1:
            return None, taken, ("%s is on %d networks (%s) - say which with "
                                 "vcenter.target.network" % (vm.name, len(nets), ", ".join(nets)))
        return nets[0], taken, None
    return None, taken, ("no VM on %s answers to %s or %s - the appliance is probably on "
                         "another host now. Point vcenter.target.host at the one running it"
                         % (host, fqdn, vm_ip))


def free_vm_name(base, target, taken):
    """A VM name the host does not already have.

    The appliance being replaced still holds its own name for the whole migration, so
    reusing it stalls the installer on its 'target appliance name is available' check
    rather than failing cleanly. Suffix with the version being moved to, which reads
    sensibly in inventory afterwards.
    """
    if base not in taken:
        return base
    digits = "".join(ch for ch in str(target) if ch.isdigit())
    stem = "%s-%s" % (base, digits or "new")
    if stem not in taken:
        return stem
    for n in range(2, 100):
        if "%s-%d" % (stem, n) not in taken:
            return "%s-%d" % (stem, n)
    return "%s-%s" % (stem, os.getpid())


def vcsa_upgrade_spec(cfg, template, temp_ip, network, name, host_pw, root_pw, sso_pw,
                      skipped=None):
    """Fill the ISO's own upgrade template. The new appliance is deployed beside the
    old one on a temporary address, the data is migrated, then it takes the original
    address and the old appliance is powered off."""
    vc, tgt = cfg["vcenter"], cfg["vcenter"]["target"]
    # Deep copy, and merge into the template's own sections rather than replacing them.
    # Replacing new_vcsa wholesale drops whatever keys that release's schema requires
    # and the installer only tells you about the first one missing.
    spec = json.loads(json.dumps(template)) if template else {"__version": "2.13.0"}
    strict = bool(template)

    def fill(parent, key, values):
        """Set our values into one section of the template.

        With a template in hand this only touches keys the template already declares.
        The schema rejects anything it does not know - new_vcsa.os takes ssh_enable on
        an upgrade but refuses password, because the appliance inherits the source's -
        and it reports one offending key per run. Following the template's own shape
        rather than a guess at the schema makes that whole class of failure go away,
        and keeps working across releases that move keys around.
        """
        if parent is None:                    # section absent; section() already said so
            return None
        if strict and key not in parent:
            if skipped is not None:
                skipped.append(key)
            return None
        node = parent.setdefault(key, {})
        for k, v in values.items():
            if not strict or k in node:
                node[k] = v
            elif skipped is not None:
                skipped.append("%s.%s" % (key, k))
        return node

    def section(name):
        """A top level section, but never one this release's template leaves out."""
        if strict and name not in spec:
            if skipped is not None:
                skipped.append(name)
            return None
        return spec.setdefault(name, {})

    if not strict or "__comments" in spec:
        spec["__comments"] = "generated by deploy_vc.py %s" % VERSION
    new = section("new_vcsa")
    fill(new, "esxi", {
        "hostname": tgt["host"], "username": "root", "password": host_pw,
        "deployment_network": network, "datastore": tgt["datastore"],
    })
    fill(new, "appliance", {
        "thin_disk_mode": True,
        "deployment_option": vc.get("size", "small"),
        "name": name,
    })
    # No password here: on an upgrade the new appliance keeps the source's root account.
    fill(new, "os", {"ssh_enable": True})
    fill(new, "temporary_network", {
        "ip_family": "ipv4", "mode": "static", "ip": temp_ip,
        "prefix": str(vc["prefix"]), "gateway": vc["gateway"],
        "dns_servers": _as_list(vc["dns"]),
    })
    fill(new, "user_options", {"vcdb_migrateSet": "core"})

    src = section("source_vc")
    # The appliance VM runs on the deployment target host, so the same credentials
    # reach it; vc_vcsa is the appliance being replaced.
    fill(src, "managing_esxi_or_vc", {
        "hostname": tgt["host"], "username": "root", "password": host_pw,
    })
    fill(src, "vc_vcsa", {
        "hostname": vc["fqdn"],
        "username": opt(cfg, "vcenter.vc_user", "administrator@vsphere.local"),
        "password": sso_pw, "root_password": root_pw,
    })
    fill(section("ceip"), "settings", {"ceip_enabled": False})
    return spec


def redact(node, marks):
    """The spec with every secret replaced, so a failed run can be looked at safely."""
    if isinstance(node, dict):
        return {k: ("********" if isinstance(v, str) and v and v in marks
                    else redact(v, marks)) for k, v in node.items()}
    if isinstance(node, list):
        return [redact(v, marks) for v in node]
    return node


def step_upgrade(cfg, st, apply_):
    """Upgrade the appliance in place, when upgrade.target asks for it."""
    r = Result("upgrade")
    target = opt(cfg, "upgrade.target")
    if unset(target):
        return r.add(SKIP, "upgrade.target is not set - the appliance is left as it is")
    fqdn = need(cfg, "vcenter.fqdn", "the appliance FQDN")
    version, build = soap_about(fqdn, timeout=5)
    if not version:
        return r.add(BLOCKED, "the vCenter is not up yet")
    if version_at_least(version, target):
        return r.add(OK, "%s is %s build %s, at or past %s" % (fqdn, version, build, target))

    installer = opt(cfg, "upgrade.installer")
    if unset(installer):
        return r.add(BLOCKED, "%s is %s and upgrade.target is %s, but upgrade.installer is not"
                              " set - mount the %s ISO and point at its vcsa-deploy"
                              % (fqdn, version, target, target))
    if not os.path.exists(installer):
        return r.add(BLOCKED, "upgrade.installer %s does not exist - is the ISO still"
                              " mounted?" % installer)
    temp_ip = opt(cfg, "vcenter.temp_ip")
    if unset(temp_ip):
        return r.add(BLOCKED, "vcenter.temp_ip is not set - the upgrade needs a free address"
                              " on the management network while it migrates")
    tgt_host = need(cfg, "vcenter.target.host", "the host to deploy the new appliance on")
    precheck = st.get("precheck")
    vendor = st.get("vendor_precheck")
    if vendor and not apply_:
        return r.add(BLOCKED, "--vendor-precheck runs the installer's own precheck, and on 9.x "
                              "that upgrades the VLCM service on %s before it will check "
                              "anything. That is a change, so it needs --commit. Plain "
                              "--precheck checks everything this tool can without touching "
                              "the appliance" % fqdn)
    if not apply_ and not precheck:
        r.add(OK, "deployment network: read off %s at commit, not taken from the config" % fqdn)
        return r.add(CHANGE, "upgrade %s from %s to %s (new appliance at %s, then it takes %s)"
                     % (fqdn, version, target, temp_ip, cfg["vcenter"]["ip"]))

    # Never from the config: the replacement must land where the appliance already is.
    network, taken, why = read_appliance(tgt_host, st["pw"].get("vcenter.target.password"),
                                         cfg["vcenter"]["ip"], fqdn)
    if network is None:
        return r.add(BLOCKED, "cannot read which portgroup %s is on: %s. Deploying onto the "
                              "wrong one leaves the new appliance unreachable, so this will "
                              "not guess" % (fqdn, why))
    configured = opt(cfg, "vcenter.target.network")
    if not unset(configured) and configured != network:
        r.add(CHANGE, "deployment network %s, read off %s - vcenter.target.network says %s, "
                      "which is stale and was ignored" % (network, fqdn, configured))
    else:
        r.add(OK, "deployment network %s, read off %s" % (network, fqdn))

    short = fqdn.split(".")[0]
    name = opt(cfg, "vcenter.target.appliance_name")
    if unset(name):
        name = free_vm_name(short, target, taken)
    if name != short:
        r.add(OK, "new appliance VM named %s - %s is still the one being replaced"
              % (name, short))

    template, path = upgrade_template(installer)
    if template is None:
        r.add(CHANGE, "no upgrade template found beside %s - using a built-in spec"
                      % os.path.basename(installer))
    else:
        r.add(OK, "template %s (__version %s)" % (os.path.basename(path),
                                                  template.get("__version", "?")))
    skipped = []
    spec = vcsa_upgrade_spec(cfg, template, temp_ip, network, name,
                             st["pw"].get("vcenter.target.password"),
                             st["pw"].get("vcenter.root_password"),
                             st["pw"].get("vcenter.vc_password"), skipped)
    if skipped:
        # Following the template means not setting what it does not declare. Say which,
        # rather than leaving a template placeholder to be discovered later.
        r.add(SKIP, "not in this template, left as it ships: %s" % ", ".join(sorted(set(skipped))))
    secrets = {q for q in (st["pw"].get("vcenter.target.password"),
                           st["pw"].get("vcenter.root_password"),
                           st["pw"].get("vcenter.vc_password")) if q}
    safe_path = os.path.join(st["outdir"], "vcsa-upgrade.redacted.json")
    try:
        with open(safe_path, "w") as fh:
            json.dump(redact(spec, secrets), fh, indent=4)
    except OSError:
        safe_path = None

    if precheck and not vendor:
        # Everything that can be known without touching anything. The installer's own
        # precheck is not read-only on 9.x, so it lives behind --vendor-precheck.
        for problem in spec_problems(spec, {
                "new_vcsa.esxi.deployment_network": network,
                "new_vcsa.appliance.name": name,
                "new_vcsa.temporary_network.ip": temp_ip}):
            r.add(BLOCKED, problem)
        free = ip_free(temp_ip)
        r.add(OK if free else (SKIP if free is None else BLOCKED),
              "temporary address %s is free" % temp_ip if free else
              ("cannot tell whether %s is free - no ping here" % temp_ip if free is None
               else "%s answers to ping - the upgrade needs it unused" % temp_ip))
        ok, detail = dns_check(fqdn, opt(cfg, "vcenter.ip"))
        r.add(OK if ok else BLOCKED, detail)
        if safe_path:
            r.add(OK, "spec built and checked, passwords removed: %s" % safe_path)
        if r.state != BLOCKED:
            # CHANGE, not OK: the checks passed but the upgrade has not happened, and a
            # run that reports ok reads as finished and points at the import.
            r.add(CHANGE, "nothing was changed. --vendor-precheck --commit for the "
                          "installer's own checks, or --commit to upgrade %s from %s to %s"
                  % (fqdn, version, target))
        return r

    lock, held_by = take_host_lock(tgt_host, os.path.dirname(st["outdir"]), st.get("site", "?"))
    if held_by:
        return r.add(BLOCKED, "another deployment onto %s is already running (%s). Two "
                              "migrations onto one host contend for it and one of them "
                              "auto-cancels - wait for that run to finish" % (tgt_host, held_by))

    spec_path = os.path.join(st["outdir"], "vcsa-upgrade.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=4)
    os.chmod(spec_path, 0o600)
    if vendor:
        r.add(CHANGE, "the installer's precheck upgrades the VLCM service on %s before it "
                      "checks anything - that change is being made now" % fqdn)
    def keep_evidence():
        """A failed run is worth looking at, so leave the spec behind with the secrets out."""
        try:
            safe = os.path.join(st["outdir"], "vcsa-upgrade.redacted.json")
            with open(safe, "w") as fh:
                json.dump(redact(spec, {p for p in (st["pw"].get("vcenter.target.password"),
                                                    st["pw"].get("vcenter.root_password"),
                                                    st["pw"].get("vcenter.vc_password")) if p}),
                          fh, indent=4)
            return " The spec it was given, passwords removed: %s" % safe
        except OSError:
            return ""

    base = [installer, "upgrade", "--accept-eula", "--no-ssl-certificate-verification"]
    try:
        if vendor:
            print("  running %s upgrade --precheck-only" % os.path.basename(installer))
            rc = subprocess.call(base + ["--precheck-only", spec_path])
            if rc != 0:
                return r.add(BLOCKED, "upgrade precheck exited %d - fix what it named above.%s"
                             % (rc, keep_evidence()))
            return r.add(OK, "upgrade precheck passed - %s is ready to go to %s" % (fqdn, target))
        print("  running %s upgrade - this takes a while and must not be interrupted"
              % os.path.basename(installer))
        rc = subprocess.call(base + [spec_path])
        # Ask the appliance either way. A reduced downtime upgrade runs inside the
        # appliance's own LCM service; vcsa-deploy starts it and then polls. Lose that
        # poll - the source stops answering on 443 partway, which is normal around
        # switchover - and the installer exits non-zero while the upgrade carries on
        # and finishes. The exit code describes the installer, not the appliance.
        now, nbuild = soap_about(fqdn, timeout=600)
        if rc != 0:
            if now and version_at_least(now, target):
                return r.add(ADD, "vcsa-deploy exited %d, but %s is now %s build %s. The "
                                  "installer lost its connection to the appliance; the upgrade "
                                  "itself completed. Check the inventory and vSAN before "
                                  "relying on it, since post-upgrade tasks were never "
                                  "confirmed.%s" % (rc, fqdn, now, nbuild, keep_evidence()))
            return r.add(BLOCKED, "vcsa-deploy upgrade exited %d and %s is %s - read its log. "
                                  "The original appliance is still there unless it reported "
                                  "otherwise.%s"
                         % (rc, fqdn, ("still " + now) if now else "not answering",
                            keep_evidence()))
        if not now:
            return r.add(BLOCKED, "the upgrade finished but %s is not answering yet" % fqdn)
        return r.add(ADD, "upgraded %s to %s build %s" % (fqdn, now, nbuild))
    finally:
        try:
            os.remove(spec_path)
        except OSError:
            pass
        if lock is not None:
            lock.close()


def _connect(cfg, st):
    if st.get("vs"):
        return st["vs"]
    fqdn = need(cfg, "vcenter.fqdn", "the appliance FQDN")
    if not soap_about(fqdn, timeout=5)[0]:
        return None
    user = opt(cfg, "vcenter.user", "administrator@" + str(opt(cfg, "vcenter.sso_domain", "vsphere.local")))
    st["vs"] = VSphere(fqdn, user, st["pw"].get("vcenter.vc_password"))
    return st["vs"]


def step_objects(cfg, st, apply_):
    r = Result("objects")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    r.add(*vs.ensure_datacenter(need(cfg, "datacenter", "the datacenter name"), apply_))
    r.add(*vs.ensure_cluster(cfg["datacenter"], need(cfg, "cluster.name", "the cluster name"),
                             opt(cfg, "cluster.ha", True), opt(cfg, "cluster.drs", True),
                             opt(cfg, "cluster.evc"), apply_,
                             seed=opt(cfg, "cluster.image_seed_from"),
                             build=opt(cfg, "esx.build")))
    return r


def step_hosts(cfg, st, apply_):
    r = Result("hosts")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    pw = st["pw"].get("esx_password") if apply_ else ""
    for h in st["hosts"]:
        r.add(*vs.ensure_host(cfg["cluster"]["name"], h, pw, apply_))
    return r


def _dvs(cfg, st, vs):
    """The switch from this run, or looked up, so a step works on its own with --only."""
    dvs = st.get("dvs")
    if dvs is None:
        dvs = vs.vds_by_name(cfg["datacenter"], need(cfg, "network.vds", "the switch name"))
        st["dvs"] = dvs
    return dvs


def step_vds(cfg, st, apply_):
    r = Result("vds")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    mtu = int(opt(cfg, "network.mtu", 9000))
    state, detail, dvs = vs.ensure_vds(cfg["datacenter"], need(cfg, "network.vds", "the switch name"),
                                       mtu, int(opt(cfg, "network.uplinks", 2)), apply_)
    r.add(state, detail)
    st["dvs"] = dvs
    if dvs is None:
        return r
    wanted = {}
    for k in ("management", "vmotion", "vsan"):
        name, vlan = opt(cfg, "network.portgroups.%s.name" % k), opt(cfg, "network.portgroups.%s.vlan" % k)
        if k == "management" and (name is None or vlan is None):
            r.add(SKIP, "no management portgroup: vmk0 stays on its standard switch, so one is "
                        "only needed for VMs")
            continue
        wanted[need(cfg, "network.portgroups.%s.name" % k, "the %s portgroup" % k)] = \
            need(cfg, "network.portgroups.%s.vlan" % k, "the %s VLAN" % k)
    for state, detail in vs.ensure_portgroups(dvs, wanted, apply_):
        r.add(state, detail)
    pnics = _as_list(opt(cfg, "network.pnics", ["vmnic0", "vmnic1"]))
    for h in st["hosts"]:
        r.add(*vs.ensure_host_on_vds(dvs, h, pnics, apply_))
    return r


def step_vmk(cfg, st, apply_):
    r = Result("vmk")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    dvs = _dvs(cfg, st, vs)
    if dvs is None:
        return r.add(BLOCKED, "the distributed switch does not exist yet - run --only vds first")
    mtu = int(opt(cfg, "network.mtu", 9000))
    for kind, stack, tag in (("vmotion", "vmotion", None), ("vsan", None, "vsan")):
        pg = need(cfg, "network.portgroups.%s.name" % kind, "the %s portgroup" % kind)
        pool = ip_range(need(cfg, "network.%s.range" % kind, "the %s address range" % kind))
        mask = netmask(need(cfg, "network.%s.prefix" % kind, "the %s prefix length" % kind))
        for h, ip in assign_ips(st["hosts"], pool).items():
            taken = vmk_conflict(vs, h, ip)
            if taken:
                r.add(BLOCKED, "%s: %s is %s. Addresses are handed out by position in "
                               "network.%s.range, and this tool reads one site at a time, "
                               "so a range that overlaps another cluster's on the same "
                               "subnet collides silently. Move this site's range."
                               % (h, ip, taken, kind))
                continue
            r.add(*vs.ensure_vmk(h, dvs, pg, ip, mask, mtu, stack, tag, apply_,
                                 gateway=opt(cfg, "network.%s.gateway" % kind)))
    return r


def vmk_conflict(vs, fqdn, ip):
    """Whether this address belongs to somebody else already.

    cluster-c and cluster-d share VLANs 161 and 162, and their ranges are kept apart
    by hand in the config. Nothing stopped a mistake there from quietly giving two
    vmkernels the same address, which is a miserable thing to debug afterwards.
    The inventory is checked first because it names the culprit; a ping only says
    somebody answered.
    """
    try:
        for host in vs._all(vs.vim.HostSystem):
            for vnic in (getattr(getattr(getattr(host, "config", None),
                                         "network", None), "vnic", None) or []):
                if getattr(getattr(vnic.spec, "ip", None), "ipAddress", None) != str(ip):
                    continue
                if host.name == fqdn:
                    return ""                 # already ours: ensure_vmk makes it idempotent
                return "already on %s as %s" % (host.name, vnic.device)
    except Exception:
        pass                                  # a read that fails is not a conflict
    return "" if ip_free(ip) is not False else "answering on the network already"


def step_mgmt(cfg, st, apply_):
    """vmk0 onto a dedicated distributed portgroup. The VCF import refuses a host without one."""
    r = Result("mgmt")
    pg = opt(cfg, "network.portgroups.management.name")
    if not pg:
        return r.add(SKIP, "no management portgroup in the config, so vmk0 stays on its "
                           "standard switch - the VCF import will refuse these hosts")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    dvs = _dvs(cfg, st, vs)
    if dvs is None:
        return r.add(BLOCKED, "the distributed switch does not exist yet - run --only vds first")
    device = str(opt(cfg, "network.management.device", "vmk0"))
    allow = bool(opt(cfg, "network.management.allow_vlan_change", False))
    # Once vmk0 is on the switch there is no standard switch left to read its nic from,
    # so the end state has to be stated rather than inferred.
    want_pnics = _as_list(opt(cfg, "network.pnics", []))
    for h in st["hosts"]:
        try:
            state, detail = vs.migrate_mgmt_vmk(h, dvs, pg, device, allow, apply_,
                                                st.get("outdir"), want_pnics)
        except Exception as e:
            # Let it escape and run() blames the whole step, losing which host it was
            # and skipping the stop-on-first-refusal below.
            state, detail = BLOCKED, ("%s: %s. What was sent and what came back: %s/mgmt-%s.*.txt"
                                      % (h, fault(e), st.get("outdir"), h))
        r.add(state, detail)
        # one host at a time, and stop on the first refusal rather than repeating a
        # bad move down the rack
        if apply_ and state == BLOCKED:
            r.add(SKIP, "stopped at %s: the hosts after it were left alone" % h)
            break
    return r


def step_image(cfg, st, apply_):
    """A vLCM image on the cluster - the VCF import will not take a baseline-managed one."""
    r = Result("image")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    r.add(*vs.ensure_cluster_image(cfg["cluster"]["name"], apply_, st.get("outdir")))
    return r


def step_vsan(cfg, st, apply_):
    r = Result("vsan")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    for state, detail in vs.ensure_vsan(cfg["cluster"]["name"],
                                        bool(opt(cfg, "cluster.ha", False)),
                                        opt(cfg, "cluster.disk_groups", "auto"), apply_):
        r.add(state, detail)
    return r


def ntp_servers(cfg):
    """The host NTP list: the site's own if given, otherwise the vCenter's.

    One site keeps one time source. Splitting them is possible and almost always a
    mistake, so the fallback is the default and ntp: at site level is the override.
    """
    return [str(s) for s in (_as_list(opt(cfg, "ntp")) or _as_list(opt(cfg, "vcenter.ntp")))]


def step_ntp(cfg, st, apply_):
    """NTP on every host. VCF checks this at import and again at every inventory sync.

    It is not cosmetic: the import refuses a host whose clock is unmanaged, and an
    inventory sync against a domain that already imported will fail the same way
    later. Both of those have cost us a session, so it is a step rather than a note.
    """
    r = Result("ntp")
    servers = ntp_servers(cfg)
    if not servers:
        return r.add(BLOCKED, "no NTP servers configured - set ntp: for this site, or "
                              "vcenter.ntp, to the time source these hosts should use")
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "the vCenter is not up yet")
    for h in st["hosts"]:
        for state, detail in vs.ensure_ntp(h, servers, apply_):
            r.add(state, detail)
    return r


def step_ssh(cfg, st, apply_):
    r = Result("ssh")
    fqdn = need(cfg, "vcenter.fqdn", "the appliance FQDN")
    if not soap_about(fqdn, timeout=5)[0]:
        return r.add(BLOCKED, "the vCenter is not up yet")
    user = opt(cfg, "vcenter.user", "administrator@" + str(opt(cfg, "vcenter.sso_domain", "vsphere.local")))
    r.add(*vcenter_ssh(fqdn, user, st["pw"].get("vcenter.vc_password"), apply_))
    return r


def step_ready(cfg, st, apply_):
    """The VCF import prerequisites, read back from the environment."""
    r = Result("ready")
    fqdn = opt(cfg, "vcenter.fqdn")
    version, build = soap_about(fqdn, timeout=5)
    if not version:
        return r.add(BLOCKED, "the vCenter is not up yet")
    r.add(OK if version >= "8.0.1" else BLOCKED,
          "vCenter %s build %s (import wants 8.0 U1 or later)" % (version, build))
    for h in st["hosts"]:
        if "." not in h:
            r.add(BLOCKED, "%s is a short name - the import requires FQDNs" % h)
    vs = _connect(cfg, st)
    if vs is None:
        return r.add(BLOCKED, "cannot log in to the vCenter")
    in_vc = {h.name for h in vs._all(vs.vim.HostSystem)}
    missing = [h for h in st["hosts"] if h not in in_vc]
    r.add(BLOCKED if missing else OK,
          "%d of %d hosts in the vCenter%s" % (len(st["hosts"]) - len(missing), len(st["hosts"]),
                                               ": missing " + ", ".join(missing) if missing else ""))
    extra = sorted(in_vc - set(st["hosts"]))
    if extra:
        r.add(SKIP, "this vCenter also holds %s - the import takes every cluster" % ", ".join(extra))
    # the import rejects any host whose management vmkernel is still on a vSwitch
    device = str(opt(cfg, "network.management.device", "vmk0"))
    stragglers, unknown = [], []
    for h in st["hosts"]:
        host = vs.find(vs.vim.HostSystem, h)
        if host is None:
            # Not in the vCenter, so nothing to read. Counting it as compliant is
            # how "0 of 8 hosts in the vCenter" came out as "8 of 8 on a dedicated
            # portgroup" in the same report.
            unknown.append(h)
        elif vs.on_dvpg(vs.mgmt_source(host, device)[0]) is None:
            stragglers.append(h)
    checked = len(st["hosts"]) - len(unknown)
    cl = vs.find(vs.vim.ClusterComputeResource, cfg["cluster"]["name"])
    if cl is None:
        # Absent is not the same as baseline-managed, and the difference decides the
        # remedy. A cluster that does not exist is not converted by --only image; it
        # is created by --only objects, already on an image when a seed is set. Saying
        # "baseline-managed" here sends the operator to a command that does nothing.
        r.add(BLOCKED, "%s does not exist yet, so it has no image. Creating it with "
                       "cluster.image_seed_from set makes it image-managed from birth - "
                       "no conversion needed" % cfg["cluster"]["name"])
    else:
        imaged = bool(getattr(cl, "lifecycleManaged", False))
        r.add(OK if imaged else BLOCKED,
              "%s is managed by a vLCM image" % cfg["cluster"]["name"] if imaged else
              "%s is baseline-managed - the import refuses that. Run --only image --commit "
              "to convert it (one way: a cluster on an image cannot go back to baselines)"
              % cfg["cluster"]["name"])
    if not checked:
        r.add(BLOCKED, "%s on a dedicated portgroup: cannot tell, none of these %d hosts "
                       "are in the vCenter yet" % (device, len(st["hosts"])))
    else:
        r.add(BLOCKED if (stragglers or unknown) else OK,
              "%s on a dedicated portgroup: %d of %d checked%s%s"
              % (device, checked - len(stragglers), checked,
                 " - run --only mgmt for " + ", ".join(stragglers) if stragglers else "",
                 "; %d not in the vCenter yet, so unchecked: %s"
                 % (len(unknown), ", ".join(unknown)) if unknown else ""))
    r.add(*shared_datastore(vs, cl, st["hosts"]))
    # NTP, because the import checks it and so does every later inventory sync. The
    # failure arrives from VCF Operations with no indication which host is at fault,
    # so name them here instead.
    want = ntp_servers(cfg)
    if not want:
        r.add(BLOCKED, "no NTP servers configured for this site - the import checks every "
                       "host's time source. Set ntp: or vcenter.ntp")
    else:
        bad, unseen = [], []
        for h in st["hosts"]:
            host = vs.find(vs.vim.HostSystem, h)
            if host is None:
                unseen.append(h)
                continue
            s = vs.ntp_state(host)
            why = []
            if s["servers"] != want:
                why.append("servers %s" % (", ".join(s["servers"]) or "none"))
            if not s["running"]:
                why.append("ntpd stopped")
            if s["policy"] != "on":
                why.append("policy %s" % (s["policy"] or "unset"))
            if s["firewall"] is False:
                why.append("ntpClient ruleset closed")
            if why:
                bad.append("%s (%s)" % (h, ", ".join(why)))
        if bad:
            r.add(BLOCKED, "NTP not right on %d host%s - run --only ntp --commit: %s"
                  % (len(bad), "" if len(bad) == 1 else "s", "; ".join(bad)))
        elif len(unseen) == len(st["hosts"]):
            r.add(BLOCKED, "NTP: cannot tell, none of these %d hosts are in the vCenter yet"
                  % len(st["hosts"]))
        elif unseen:
            r.add(BLOCKED, "NTP is right where it could be read, but %d host%s not in the "
                           "vCenter yet, so unchecked: %s"
                  % (len(unseen), " is" if len(unseen) == 1 else "s are", ", ".join(unseen)))
        else:
            r.add(OK, "NTP %s, running and set to start with the host, on every host"
                  % ", ".join(want))
    return r


def shared_datastore(vs, cl, hosts):
    """A datastore every host in the cluster can see.

    The import's "Cluster common datastore check" wants one. This step used to pass
    on cluster-c while vSAN was switched off on the cluster - the disks were still
    claimed, so nothing else looked wrong, and only the VCF wizard caught it. Local
    VMFS does not count: one per host is not shared.
    """
    if cl is None:
        return BLOCKED, "cluster does not exist yet, so there is no datastore to share"
    seen = {}
    for host in (cl.host or []):
        for ds in (getattr(host, "datastore", None) or []):
            seen.setdefault(ds.name, set()).add(host.name)
    everyone = sorted(n for n, where in seen.items() if len(where) == len(cl.host or []))
    if everyone:
        return OK, "a datastore every host can see: %s" % ", ".join(everyone)
    if not seen:
        return BLOCKED, ("no datastores at all on these hosts. If this cluster is vSAN, "
                         "check vSAN is still enabled on it - the disks can stay claimed "
                         "while the service is off. Run --only vsan to see")
    widest = max(seen.items(), key=lambda kv: len(kv[1]))
    return BLOCKED, ("no datastore is visible to all %d hosts, so the import's common "
                     "datastore check will fail. The best is %s, on %d of them. If this "
                     "cluster is vSAN, check vSAN is enabled and every host is in it - "
                     "run --only vsan"
                     % (len(cl.host or []), widest[0], len(widest[1])))


STEP_FUNCS = {
    "dns": step_dns, "esx": step_esx, "vcsa": step_vcsa, "upgrade": step_upgrade, "objects": step_objects,
    "hosts": step_hosts, "ntp": step_ntp, "vds": step_vds, "vmk": step_vmk, "mgmt": step_mgmt,
    "vsan": step_vsan, "image": step_image, "ssh": step_ssh, "ready": step_ready,
}


# ---------------------------------------------------------------- driver
class Tee:
    """The screen, and the run's console.txt beside it."""

    def __init__(self, path):
        self.f = open(path, "w")
        self.out = sys.stdout

    def write(self, text):
        self.out.write(text)
        self.f.write(text)

    def flush(self):
        self.out.flush()
        self.f.flush()


def run(cfg, steps, commit, st):
    results = []
    for name in steps:
        print("==== %s" % name)
        try:
            res = STEP_FUNCS[name](cfg, st, commit)
        except SystemExit:
            raise
        except Exception as e:
            res = Result(name).add(BLOCKED, "%s: %s" % (type(e).__name__, fault(e)))
        for state, detail in res.rows:
            print("  %-7s %s" % (state.upper(), detail))
        results.append(res)
        print()
    return results


def table(results, commit):
    """Prints one line per step; returns (blocked, pending)."""
    print("==== result" if commit else "==== plan")
    width = max([len(r.step) for r in results] + [4])
    print("%-*s  %-8s  %s" % (width, "STEP", "STATE", "DETAIL"))
    for r in results:
        print("%-*s  %-8s  %s" % (width, r.step, r.state, r.summary))
    blocked = sum(1 for r in results if r.state == BLOCKED)
    pending = sum(1 for r in results if r.state in (ADD, CHANGE))
    return blocked, pending


def next_line(results, commit, cmd):
    blocked = [r for r in results if r.state == BLOCKED]
    pending = [r for r in results if r.state in (ADD, CHANGE)]
    if blocked:
        return "NEXT: clear %s, then %s" % (blocked[0].step, cmd)
    if pending:
        return "NEXT: %s --commit" % cmd if not commit else "NEXT: %s" % cmd
    # A clean --only run says nothing about the steps that did not run, and sending
    # someone to the import on the strength of one green step is worse than silence.
    rest = [s for s in STEPS if s not in {r.step for r in results}]
    if rest:
        return "NEXT: %s is clean. Not run: %s" % (
            ", ".join(r.step for r in results), " ".join(rest))
    return "NEXT: VCF Operations -> Add Workload Domain -> Import a vCenter"


def exit_code(blocked, pending, commit, detailed):
    """1 blocked, 2 a plan with changes pending and --detailed-exitcode, else 0."""
    if blocked:
        return 1
    if detailed and not commit and pending:
        return 2
    return 0


def system_for(key):
    return {"vcenter.vc_password": "the vCenter",
            "vcenter.root_password": "root on the NEW vCenter appliance (you are choosing it)",
            "vcenter.target.password": "root on the host the appliance deploys onto",
            "esx_password": "root on the ESX hosts"}.get(key, key)


def build(args):
    if not os.path.exists(args.config):
        die("config not found: %s" % args.config)
    if args.set:
        name, eq, value = args.set.partition("=")
        name = name.strip()
        if not eq:
            if not sys.stdin.isatty():
                die("--set %s needs a terminal to ask on, or NAME=VALUE" % name)
            one = getpass.getpass("      value for %s: " % name)
            two = getpass.getpass("      again: ")
            if one != two:
                die("they do not match - nothing changed")
            value = "'%s'" % one.replace("'", "''")
        backup = set_value(args.config, name, value.strip())
        readback = load_config(args.config)
        node, ok = readback, True
        for part in name.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        print("%s set in %s (the file before it is %s)" % (name, args.config, backup))
        print("it reads back as expected" if ok else "WARNING: it does not read back - check the file")
        return 0 if ok else 1

    cfg = load_config(args.config)
    site = pick_site(cfg, args.site)
    scfg = site_config(cfg, site)
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(args.outdir or opt(cfg, "outdir", "/tmp/deploy_vc"), stamp)
    os.makedirs(outdir, exist_ok=True)
    sys.stdout = Tee(os.path.join(outdir, "console.txt"))

    print("deploy_vc.py v%s   %s   site %s   %s" % (
        VERSION, args.config, site,
        "COMMIT - changes will be made" if args.commit else "PLAN - nothing will be changed"))
    print("evidence: %s" % outdir)
    print()
    hosts = [str(h) for h in _as_list(need(scfg, "hosts", "the ESX hosts for this cluster"))]
    st = {"hosts": hosts, "outdir": outdir, "site": site,
          "precheck": args.precheck or args.vendor_precheck,
          "vendor_precheck": args.vendor_precheck,
          "pw": Secrets(scfg, site, system_for)}
    results = run(scfg, args.only or STEPS, args.commit, st)
    blocked, pending = table(results, args.commit)
    print()
    cmd = "python3 %s build --config %s%s" % (
        os.path.basename(__file__), args.config, " --site %s" % site if args.site else "")
    print(next_line(results, args.commit, cmd))
    return exit_code(blocked, pending, args.commit, args.detailed_exitcode)


def _has_pyvmomi(python):
    try:
        return subprocess.call([python, "-c", "import pyVmomi"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except Exception:
        return False


def candidate_pythons():
    """Interpreters that might carry pyvmomi, nearest first.

    Covers the venv in use, one beside the script, the usual spots under the home
    directory, and any <home>/*/venv - which is where these end up in practice.
    """
    import glob
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    if os.environ.get("VIRTUAL_ENV"):
        out.append(os.path.join(os.environ["VIRTUAL_ENV"], "bin", "python3"))
    for d in (here, home, os.path.join(home, ".deploy_vc")):
        for name in ("venv", ".venv"):
            out.append(os.path.join(d, name, "bin", "python3"))
    for pat in ("*/venv/bin/python3", "*/.venv/bin/python3"):
        out.extend(sorted(glob.glob(os.path.join(home, pat))))
    seen, uniq = set(), []
    for q in out:
        if q not in seen and q != sys.executable and os.path.isfile(q):
            seen.add(q)
            uniq.append(q)
    return uniq


def ensure_pyvmomi():
    """Re-run under a Python that has pyvmomi, whichever one was typed.

    Remembering which interpreter holds the library is only the operator's problem
    because the tool makes it theirs. Go and find one instead. os.execv keeps the
    terminal, so password prompts still work.
    """
    try:
        import pyVmomi                                           # noqa: F401
        return None
    except ImportError:
        pass
    if os.environ.get("DEPLOY_VC_NO_REEXEC"):
        return None
    for python in candidate_pythons():
        if _has_pyvmomi(python):
            print("pyvmomi is not in %s - re-running under %s\n" % (sys.executable, python))
            os.environ["DEPLOY_VC_NO_REEXEC"] = "1"
            os.execv(python, [python, os.path.abspath(__file__)] + sys.argv[1:])
    return ("no Python on this machine has pyvmomi. One command fixes it for good:\n"
            "  python3 %s bootstrap" % os.path.abspath(__file__))


def bootstrap(where=None):
    """Build the virtualenv the tool will find by itself from then on."""
    where = os.path.expanduser(where or os.path.join("~", ".deploy_vc", "venv"))
    python = os.path.join(where, "bin", "python3")
    if not _has_pyvmomi(python):
        if not os.path.isfile(python):
            print("creating %s" % where)
            if subprocess.call([sys.executable, "-m", "venv", where]) != 0:
                die("could not create a virtualenv at %s" % where)
        print("installing pyvmomi into %s" % where)
        if subprocess.call([os.path.join(where, "bin", "pip"), "install", "--quiet",
                            "pyvmomi"]) != 0:
            die("pip could not install pyvmomi into %s - is PyPI reachable from here?" % where)
        if not _has_pyvmomi(python):
            die("pyvmomi still will not import from %s" % python)
    print("ready: %s" % python)
    print("deploy_vc.py finds this on its own now - run your build command with any python.")
    return 0


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(prog="deploy_vc.py", add_help=False)
    p.add_argument("-h", "--help", action="store_true")
    sub = p.add_subparsers(dest="command")
    b = sub.add_parser("build", add_help=False, help="stand up the vSphere 8 environment for a site")
    b.add_argument("--config", default=os.path.join(here, "env_vc8.yaml"), help="config file (YAML/JSON)")
    b.add_argument("--site", help="which site to build (default: the first under sites:)")
    b.add_argument("--commit", action="store_true", help="actually write. Without this nothing is changed.")
    b.add_argument("--only", action="append", choices=STEPS, help="one step; repeatable")
    b.add_argument("--precheck", action="store_true",
                   help="check everything that can be checked without touching anything: "
                        "the spec, the temporary address, DNS, the appliance's own network "
                        "and name. Writes no spec, runs no installer, changes nothing.")
    b.add_argument("--vendor-precheck", action="store_true",
                   help="also run vcsa-deploy's own --precheck-only. On 9.x upgrades that "
                        "first updates the VLCM service on the source appliance, so it is a "
                        "change and needs --commit.")
    b.add_argument("--detailed-exitcode", action="store_true",
                   help="exit 2 when a plan has changes pending (as terraform plan -detailed-exitcode)")
    b.add_argument("--set", metavar="NAME[=VALUE]",
                   help="put one value in the config by its full name, e.g. "
                        "sites.<site>.vcenter.vc_password (a password is asked for twice, never shown); "
                        "nothing else in the file changes; then stop")
    b.add_argument("--outdir", help="where this run's evidence goes (default /tmp/deploy_vc)")
    b.add_argument("-h", "--help", action="store_true")
    sub.add_parser("selftest", add_help=False, help="offline checks of the logic; touches no network")
    bs = sub.add_parser("bootstrap", add_help=False,
                        help="make a virtualenv with pyvmomi that the tool finds by itself")
    bs.add_argument("--path", help="where to put it (default ~/.deploy_vc/venv)")
    bs.add_argument("-h", "--help", action="store_true")
    args = p.parse_args(argv)
    if getattr(args, "help", False) or not args.command:
        print(__doc__)
        return 2
    if args.command == "bootstrap":
        return bootstrap(args.path)
    missing = ensure_pyvmomi()          # may replace this process; never returns if it does
    if missing and args.command != "selftest":
        print(missing + "\n")
    if args.command == "selftest":
        return selftest()
    return build(args)


# ---------------------------------------------------------------- selftest
EXAMPLE = """
# comment line
vcenter:
  fqdn: vc8.example.com
  ip: 10.0.0.60
  prefix: 24
  gateway: 10.0.0.254
  dns: [10.0.0.10, 10.0.0.11]
  ntp: time.example.com
  sso_domain: vsphere.local
  size: small
  target:
    host: esxmgmt01.example.com
    datastore: "mgmt ds 1"
    network: pg-mgmt
datacenter: dc01
cluster:
  name: cl01
  ha: true
  drs: true
esx:
  build: "12345678"
network:
  vds: vds01
  mtu: 9000
  uplinks: 2
  pnics: [vmnic0, vmnic1]
  management:
    device: vmk0
    allow_vlan_change: false
  portgroups:
    management:
      name: vds01-pg-mgmt
      vlan: 110
    vmotion:
      name: vds01-pg-vmotion
      vlan: 141
    vsan:
      name: vds01-pg-vsan
      vlan: 142
  vmotion:
    prefix: 24
    range: 10.0.141.61-99
  vsan:
    prefix: 24
    range: 10.0.142.61-10.0.142.99
hosts:
  - esx01.example.com
  - esx02.example.com
"""


SITES_EXAMPLE = """
esx:
  build: "12345678"
  user: root
sites:
  site-a:
    vcenter:
      fqdn: vc8a.example.com
      ip: 10.0.0.60
      prefix: 24
      gateway: 10.0.0.254
      dns: [10.0.0.10]
      sso_domain: vsphere.local
      size: small
      vc_user: administrator@vsphere.local
      vc_password:                           # vc8a.example.com. Blank: asked for
      root_password:                         # root on the vc8a.example.com appliance. Blank: asked for
      target:
        host: esxmgmt01.example.com
        datastore: ds1
        network: pg-mgmt
    datacenter: dc01
    cluster:
      name: cluster01
      ha: true
      drs: true
    network:
      vds: vds01
      mtu: 9000
      uplinks: 2
      pnics: [vmnic0, vmnic1]
      portgroups:
        management:
          name: vds01-pg-mgmt
          vlan: 110
        vmotion:
          name: vds01-pg-vmotion
          vlan: 141
        vsan:
          name: vds01-pg-vsan
          vlan: 142
      vmotion:
        prefix: 24
        range: 10.0.141.61-99
      vsan:
        prefix: 24
        range: 10.0.142.61-99
    esx_password:                            # root on the hosts below. Blank: asked for
    hosts:
      - esx01.example.com
      - esx02.example.com
  site-b:
    vcenter:
      fqdn: vc8b.example.com
    datacenter: dc02
    cluster:
      name: cluster02
    hosts:
      - esx11.example.com
"""


class _FakePw:
    def get(self, key, system=None):
        return "pw"


def _vim_stub():
    """Enough of the vim namespace for the managed-object lookups, without pyVmomi."""
    return type("vim", (), {"ClusterComputeResource": object, "HostSystem": object})


def _vapi_task_cases():
    """Vapi.task: poll /cis/tasks until terminal, and turn a failure into one line."""
    seen = []

    def client(script):
        v = Vapi.__new__(Vapi)
        v.base, v.ctx, v.sid = "https://vc/api", None, "sid"
        steps = list(script)
        def call(method, path, body=None, timeout=300):
            seen.append(path)
            return steps.pop(0)
        v.call = call
        return v

    real_sleep, time.sleep = time.sleep, lambda s: None
    try:
        v = client(["task-1", {"status": "RUNNING"}, {"status": "SUCCEEDED", "result": {"a": 1}}])
        ok = v.task("PUT", "/x") == {"a": 1}
        ok = ok and seen[0] == "/x?vmw-task=true" and seen[1] == "/cis/tasks/task-1"
        v = client(["task-2", {"status": "FAILED",
                               "error": {"messages": [{"default_message": "not stateful"}]}}])
        try:
            v.task("PUT", "/x")
            ok = False
        except RuntimeError as e:
            ok = ok and "not stateful" in str(e)
        # a path that already carries a query keeps it: the flag joins with &
        del seen[:]
        v = client(["task-3", {"status": "SUCCEEDED"}])
        v.task("POST", "/x?action=check")
        ok = ok and seen[0] == "/x?action=check&vmw-task=true"
        v = client([{"enabled": True}])          # a build that answers synchronously
        ok = ok and v.task("GET", "/x") == {"enabled": True}
        return ok
    finally:
        time.sleep = real_sleep


class _FakeVS:
    """Stands in for VSphere so the step wiring is exercised with no vCenter."""

    class _vim:
        class HostSystem:
            pass

        class ClusterComputeResource:
            pass

        class host:
            class NtpConfig:
                def __init__(self, server=()):
                    self.server = list(server)

            class DateTimeConfig:
                def __init__(self, ntpConfig=None):
                    self.ntpConfig = ntpConfig

    def __init__(self, present=()):
        self.vim = self._vim
        self.present = set(present)
        self.mutations = []

    def _record(self, what, apply_):
        if apply_:
            self.mutations.append(what)
        return (ADD, ("did " if apply_ else "would ") + what)

    def ensure_datacenter(self, name, apply_):
        return self._record("datacenter %s" % name, apply_)

    def ensure_cluster(self, dc, name, ha, drs, evc, apply_, seed=None, build=None):
        return self._record("cluster %s%s" % (name, " seeded from %s" % seed if seed else ""),
                            apply_)

    def ensure_host(self, cluster, fqdn, pw, apply_):
        return self._record("host %s" % fqdn, apply_)

    def ensure_vds(self, dc, name, mtu, uplinks, apply_):
        s, d = self._record("vds %s mtu %d" % (name, mtu), apply_)
        return s, d, object()

    def ensure_portgroups(self, dvs, wanted, apply_):
        return [self._record("portgroup %s vlan %s" % (n, v), apply_) for n, v in wanted.items()]

    def ensure_host_on_vds(self, dvs, fqdn, pnics, apply_):
        if getattr(self, "mgmt_pnic", None) in pnics:
            return BLOCKED, ("%s: %s carries the management vmkernel on its standard switch - "
                             "moving it would cut the host off" % (fqdn, self.mgmt_pnic))
        return self._record("attach %s (%s)" % (fqdn, ",".join(pnics)), apply_)

    def ensure_vmk(self, fqdn, dvs, pg, ip, mask, mtu, stack, tag, apply_, gateway=None):
        return self._record("vmk %s %s %s mask %s stack %s tag %s" % (fqdn, pg, ip, mask, stack, tag), apply_)

    def ensure_vsan(self, cluster, want_ha, groups, apply_):
        rows = [self._record("vsan %s" % cluster, apply_)]
        if want_ha:
            rows.append(self._record("ha on %s" % cluster, apply_))
        return rows

    def vds_by_name(self, dc, name):
        return object()

    def ntp_state(self, host):
        return dict({"servers": ["10.0.0.10", "10.0.0.11"], "running": True,
                     "policy": "on", "present": True, "firewall": True},
                    **getattr(self, "ntp", {}))

    def ensure_ntp(self, fqdn, servers, apply_):
        if fqdn not in self.present:
            return [(BLOCKED, "%s is not in this vCenter yet" % fqdn)]
        s = self.ntp_state(None)
        if s["servers"] == servers and s["running"] and s["policy"] == "on":
            return [(OK, "%s: NTP %s" % (fqdn, ", ".join(servers)))]
        return [self._record("ntp on %s -> %s" % (fqdn, ",".join(servers)), apply_)]

    def ensure_cluster_image(self, cluster, apply_, evidence=None):
        if getattr(self, "imaged", False):
            return OK, "%s is managed by a vLCM image" % cluster
        if getattr(self, "host_down", None):
            return BLOCKED, ("%s cannot convert to an image while %s is disconnected"
                             % (cluster, self.host_down))
        if not apply_:
            return ADD, ("%s: convert from baselines to a vLCM image over the Image Manager "
                         "enablement API. One way, so this needs --commit" % cluster)
        return ADD, "%s is now managed by a vLCM image" % cluster

    def migrate_mgmt_vmk(self, fqdn, dvs, pg_name, device, allow_vlan_change, apply_,
                         evidence=None, want_pnics=()):
        if fqdn in getattr(self, "already", ()):
            return OK, "%s: %s on %s" % (fqdn, device, pg_name)
        if getattr(self, "vlan_clash", False) and not allow_vlan_change:
            return BLOCKED, ("%s: %s is on VLAN 0 today, %s is VLAN 130. Moving it strands the "
                             "host" % (fqdn, device, pg_name))
        return self._record("mgmt %s %s -> %s" % (fqdn, device, pg_name), apply_)

    # -- what step_ready reads back
    def find(self, kind, name, root=None):
        if kind is getattr(self.vim, "ClusterComputeResource", None):
            if getattr(self, "no_cluster", False):
                return None
            shared = type("DS", (), {"name": "%s-vsan01" % name})()
            members = [type("H", (), {"name": h, "datastore":
                                      [] if getattr(self, "no_datastore", False)
                                      else [shared]})()
                       for h in self.present]
            return type("C", (), {"name": name, "host": members,
                                  "lifecycleManaged": getattr(self, "imaged", False)})()
        return type("H", (), {"name": name})() if name in self.present else None

    def on_dvpg(self, vnic):
        return vnic

    def mgmt_source(self, host, device):
        return (object() if host.name in getattr(self, "already", ()) else None,
                None, None, [], None)

    def _all(self, kind):
        return [type("H", (), {"name": n})() for n in self.present]


def selftest():
    checks, failed = [], []

    def ck(name, cond, saw=None):
        checks.append(name)
        if not cond:
            failed.append(name)
        print("%-4s %s%s" % ("PASS" if cond else "FAIL", name,
                             "" if cond or saw is None else "  <- %r" % (saw,)))

    cfg = parse_yaml(EXAMPLE)

    ck("yaml: nested maps", cfg["vcenter"]["target"]["host"] == "esxmgmt01.example.com")
    ck("yaml: quoted value keeps its spaces", cfg["vcenter"]["target"]["datastore"] == "mgmt ds 1")
    ck("yaml: inline list", cfg["vcenter"]["dns"] == ["10.0.0.10", "10.0.0.11"])
    ck("yaml: block list of scalars", cfg["hosts"] == ["esx01.example.com", "esx02.example.com"])
    ck("yaml: ints and bools", cfg["network"]["mtu"] == 9000 and cfg["cluster"]["ha"] is True)
    ck("yaml: quoted digits stay a string", cfg["esx"]["build"] == "12345678")
    ck("yaml: comments and blank lines ignored", "comment" not in str(cfg))
    ck("yaml: deep portgroup map", cfg["network"]["portgroups"]["vsan"]["vlan"] == 142)

    if "yaml" in sys.modules or _has_pyyaml():
        import yaml
        ck("yaml: built-in parser agrees with PyYAML", yaml.safe_load(EXAMPLE) == cfg)

    ck("need: fetches a dotted key", need(cfg, "network.portgroups.vmotion.vlan", "x") == 141)
    ck("opt: missing key gives the default", opt(cfg, "network.nope", "dflt") == "dflt")

    ck("ip_range: short form", ip_range("10.0.141.61-99")[-1] == "10.0.141.99")
    ck("ip_range: long form", ip_range("10.0.142.61-10.0.142.99")[0] == "10.0.142.61")
    ck("ip_range: counts correctly", len(ip_range("10.0.141.61-99")) == 39)
    ck("ip_range: single address", ip_range("10.0.0.5") == ["10.0.0.5"])
    ck("netmask: /24", netmask(24) == "255.255.255.0")

    pool = ip_range("10.0.141.61-99")
    mapping = assign_ips(cfg["hosts"], pool)
    ck("assign_ips: stable by position", mapping["esx01.example.com"] == "10.0.141.61"
       and mapping["esx02.example.com"] == "10.0.141.62")

    spec = vcsa_spec(cfg, "TGTPW", "ROOTPW", "SSOPW")
    ck("vcsa spec: system name and address", spec["new_vcsa"]["network"]["system_name"] == "vc8.example.com"
       and spec["new_vcsa"]["network"]["ip"] == "10.0.0.60")
    ck("vcsa spec: appliance name is the short name", spec["new_vcsa"]["appliance"]["name"] == "vc8")
    ck("vcsa spec: ssh on, as the import needs", spec["new_vcsa"]["os"]["ssh_enable"] is True)
    ck("vcsa spec: ntp carried as a list", spec["new_vcsa"]["os"]["ntp_servers"] == ["time.example.com"])
    ck("vcsa spec: passwords only in the password fields",
       json.dumps(spec).count("ROOTPW") == 1 and json.dumps(spec).count("SSOPW") == 1
       and json.dumps(spec).count("TGTPW") == 1)
    ck("vcsa spec: the target host's root is not reused as the appliance root",
       spec["new_vcsa"]["esxi"]["password"] == "TGTPW"
       and spec["new_vcsa"]["os"]["password"] == "ROOTPW")
    ck("vcsa spec: ceip off", spec["ceip"]["settings"]["ceip_enabled"] is False)

    ck("diff_names: splits wanted from existing", diff_names(["a", "b"], ["b"]) == (["a"], ["b"]))

    r = Result("x").add(OK, "fine").add(ADD, "new")
    ck("result: add outranks ok", r.state == ADD)
    ck("result: blocked outranks everything", r.add(BLOCKED, "bad").state == BLOCKED)
    ck("result: summary names the blocker", "bad" in r.summary)

    body = "<x><version>8.0.3</version><build>12345678</build></x>"
    ck("soap: version parsed", _tag(body, "version") == "8.0.3")
    ck("soap: build parsed", _tag(body, "build") == "12345678")
    ck("soap: missing tag is None", _tag(body, "nope") is None)

    # --- steps, with the network and vCenter stubbed out
    g = globals()
    saved = {k: g[k] for k in ("dns_check", "soap_about", "_connect")}
    try:
        g["dns_check"] = lambda n, expect_ip=None: (n != "esx02.example.com",
                                                    "%s checked" % n)
        g["soap_about"] = lambda h, timeout=8: (("8.0.3", "12345678") if h.startswith("esx")
                                                else ("8.0.3", "24000000"))
        st = {"hosts": cfg["hosts"], "outdir": "/tmp", "pw": _FakePw()}

        res = step_dns(cfg, st, False)
        ck("step dns: a bad record blocks the step", res.state == BLOCKED)

        res = step_esx(cfg, dict(st), False)
        ck("step esx: matching build passes", res.state == OK)
        bad = dict(cfg)
        bad["esx"] = {"build": "99999999"}
        ck("step esx: wrong build blocks", step_esx(bad, dict(st), False).state == BLOCKED)

        fake = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fake
        st2 = dict(st)
        for fn in (step_objects, step_hosts, step_vds, step_vmk, step_vsan):
            fn(cfg, st2, False)
        ck("plan changes nothing", fake.mutations == [])

        st3 = dict(st)
        fake2 = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fake2
        step_vds(cfg, st3, True)
        ck("apply: switch, three portgroups and both hosts attached",
           sum(1 for m in fake2.mutations if m.startswith("vds")) == 1
           and sum(1 for m in fake2.mutations if m.startswith("portgroup")) == 3
           and sum(1 for m in fake2.mutations if m.startswith("attach")) == 2)
        ck("apply: portgroup VLANs come from the config",
           any("vlan 142" in m for m in fake2.mutations))

        nomgmt = json.loads(json.dumps(cfg))
        nomgmt["network"]["portgroups"]["management"] = {"name": "[set]", "vlan": "[set]"}
        fake2b = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fake2b
        res = step_vds(nomgmt, dict(st), True)
        ck("vds: an unanswered management portgroup is skipped, not guessed",
           sum(1 for m in fake2b.mutations if m.startswith("portgroup")) == 2
           and any("vmk0 stays on its standard switch" in d for _, d in res.rows))

        clash = _FakeVS(present=cfg["hosts"])
        clash.mgmt_pnic = "vmnic0"
        g["_connect"] = lambda c, s: clash
        res = step_vds(cfg, dict(st), True)
        ck("vds: the uplink carrying vmk0 is refused, not taken",
           res.state == BLOCKED and any("cut the host off" in d for _, d in res.rows)
           and not any(m.startswith("attach") for m in clash.mutations))

        fake3 = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fake3
        st4 = dict(st)
        st4["dvs"] = object()
        step_vmk(cfg, st4, True)
        vmks = [m for m in fake3.mutations if m.startswith("vmk")]
        ck("apply: one vMotion and one vSAN vmkernel per host", len(vmks) == 4)
        ck("apply: vMotion uses its own TCP/IP stack", sum(1 for m in vmks if "stack vmotion" in m) == 2)
        ck("apply: vSAN vmkernel is tagged for vSAN", sum(1 for m in vmks if "tag vsan" in m) == 2)
        ck("apply: addresses come from the configured ranges",
           any("10.0.141.61" in m for m in vmks) and any("10.0.142.62" in m for m in vmks))
        ck("apply: mask derived from the prefix", all("mask 255.255.255.0" in m for m in vmks))

        # ---- mgmt: vmk0 onto the distributed switch
        fm = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fm
        res = step_mgmt(cfg, dict(st), False)
        ck("mgmt: a plan moves every host and changes nothing",
           len(res.rows) == 2 and fm.mutations == [])

        fm2 = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fm2
        step_mgmt(cfg, dict(st), True)
        ck("mgmt: commit moves one vmkernel per host",
           len([m for m in fm2.mutations if m.startswith("mgmt")]) == 2)
        ck("mgmt: the move targets the configured management portgroup",
           all("vds01-pg-mgmt" in m for m in fm2.mutations if m.startswith("mgmt")))

        fm3 = _FakeVS(present=cfg["hosts"])
        fm3.already = set(cfg["hosts"])
        g["_connect"] = lambda c, s: fm3
        res = step_mgmt(cfg, dict(st), True)
        ck("mgmt: a second run is a no-op, not a second move",
           res.state == OK and fm3.mutations == [])

        fm4 = _FakeVS(present=cfg["hosts"])
        fm4.vlan_clash = True
        g["_connect"] = lambda c, s: fm4
        res = step_mgmt(cfg, dict(st), True)
        ck("mgmt: a VLAN that would strand the host is refused, and the rest are left alone",
           res.state == BLOCKED and fm4.mutations == []
           and any("left alone" in d for _, d in res.rows))

        vlanok = json.loads(json.dumps(cfg))
        vlanok["network"]["management"] = {"allow_vlan_change": True}
        fm5 = _FakeVS(present=cfg["hosts"])
        fm5.vlan_clash = True
        g["_connect"] = lambda c, s: fm5
        step_mgmt(vlanok, dict(st), True)
        ck("mgmt: allow_vlan_change lets the move through",
           len([m for m in fm5.mutations if m.startswith("mgmt")]) == 2)

        nopg = json.loads(json.dumps(cfg))
        nopg["network"]["portgroups"]["management"] = {"name": "[set]", "vlan": "[set]"}
        fm6 = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fm6
        res = step_mgmt(nopg, dict(st), True)
        ck("mgmt: no management portgroup means skip, never a guess",
           res.state == SKIP and fm6.mutations == [])

        ck("mgmt: runs after vds and vmk, before vsan",
           STEPS.index("mgmt") > STEPS.index("vds") and STEPS.index("mgmt") > STEPS.index("vmk")
           and STEPS.index("mgmt") < STEPS.index("vsan"))

        # ---- the vLCM image: the import refuses a baseline-managed cluster
        fi = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fi
        res = step_image(cfg, dict(st), False)
        ck("image: a plan changes nothing and says what it would convert",
           res.state == ADD and fi.mutations == [] and "vLCM image" in res.rows[0][1])
        ck("image: and says it needs --commit, because the change is one way",
           "--commit" in res.rows[0][1])
        fid = _FakeVS(present=cfg["hosts"])
        fid.host_down = "esx02.example.com"
        g["_connect"] = lambda c, s: fid
        res = step_image(cfg, dict(st), False)
        ck("image: a disconnected host is caught before anyone starts a one-way change",
           res.state == BLOCKED and "disconnected" in res.rows[0][1])
        fi3 = _FakeVS(present=cfg["hosts"])
        fi3.imaged = True
        g["_connect"] = lambda c, s: fi3
        res = step_image(cfg, dict(st), True)
        ck("image: a cluster already on an image is left alone",
           res.state == OK and fi3.mutations == [])
        ck("image: runs after the cluster is built and before the import checks",
           STEPS.index("image") > STEPS.index("hosts")
           and STEPS.index("image") < STEPS.index("ready"))

        # ---- the real enablement call, against a stubbed REST side. The fake above
        # replaces the method wholesale, so only this exercises the API it drives.
        def imaging(enabled=False, fail=None, flips=True, check_fails=False):
            calls = []

            class RestStub:
                def call(self, method, path, body=None, timeout=300):
                    calls.append((method, path, body))
                    if method == "GET":
                        return {"enabled": state["enabled"]}
                    raise AssertionError("a mutation must go through task(), not call()")

                def task(self, method, path, body=None, minutes=60):
                    calls.append((method, path, body))
                    if method == "POST" and check_fails:
                        raise RuntimeError("check refused")
                    if method == "PUT":
                        if fail:
                            raise RuntimeError(fail)
                        if flips:
                            state["enabled"] = True
                    return {"notifications": {"info": [{"message": "all hosts eligible"}]}}

            state = {"enabled": enabled}
            host = type("H", (), {"name": "esx01.example.com",
                                  "runtime": type("R", (), {"connectionState": "connected"})(),
                                  "config": type("C", (), {"product": type("P", (), {
                                      "version": "8.0.3", "build": "24585383"})()})()})()
            cl = type("CL", (), {"_moId": "domain-c8", "host": [host],
                                 "lifecycleManaged": enabled})()
            vs = VSphere.__new__(VSphere)
            vs.vim = _vim_stub()
            vs.find = lambda kind, name, root=None: cl
            vs.vapi = lambda: RestStub()
            return vs, calls, state

        vs, calls, _ = imaging()
        state, detail = vs.ensure_cluster_image("cluster-a", False)
        ck("image API: a plan reads the state and sends no mutation",
           state == ADD and [c[0] for c in calls] == ["GET"])
        ck("image API: the plan names the build the hosts run, which becomes the image",
           "8.0.3 build 24585383" in detail)

        vs, calls, state_ = imaging()
        state, detail = vs.ensure_cluster_image("cluster-a", True)
        ck("image API: a commit checks first, then enables, then reads back",
           state == ADD and [c[0] for c in calls] == ["GET", "POST", "PUT", "GET"])
        ck("image API: the check is the documented read-only action",
           "action=check" in calls[1][1])
        ck("image API: the check spec carries checks_to_skip, which is required",
           calls[1][2] == {"checks_to_skip": []})
        ck("image API: enable is a PUT to the enablement resource with the documented spec",
           calls[2][1] == "/esx/settings/clusters/domain-c8/enablement/software"
           and calls[2][2] == {"skip_software_check": False})
        ck("image API: and it says not to remediate afterwards",
           "Do not remediate" in detail and state_["enabled"] is True)

        vs, calls, _ = imaging(enabled=True)
        state, detail = vs.ensure_cluster_image("cluster-a", True)
        ck("image API: a cluster already on an image is read once and left alone",
           state == OK and [c[0] for c in calls] == ["GET"])

        vs, calls, _ = imaging(fail="depot has no base image")
        state, detail = vs.ensure_cluster_image("cluster-a", True)
        ck("image API: a refused enable reports the reason and the vSphere Client path",
           state == BLOCKED and "depot has no base image" in detail
           and "Setup Image" in detail and "esx01.example.com" in detail)

        vs, _c, _ = imaging(fail="Cannot enable the feature because one or more eligibility "
                                 "conditions were not met.; Transport Node Profile is not "
                                 "configured on the cluster but NSX-T Data Center is installed "
                                 "on the following host(s): esx01")
        state, detail = vs.ensure_cluster_image("cluster-c", True)
        ck("image API: the NSX condition sends you to NSX Manager, not round the UI loop",
           state == BLOCKED and "Configure NSX" in detail and "Setup Image" not in detail)
        ck("image API: and says the profile probably already exists, because VCF detaches it",
           "usually detaches" in detail and "cluster-c" in detail)
        vs, _c, _ = imaging(fail="the cluster contains stateless hosts")
        state, detail = vs.ensure_cluster_image("cluster-c", True)
        ck("image API: a stateless cluster is told it cannot convert, not where to click",
           state == BLOCKED and "stateless" in detail and "Setup Image" not in detail)
        vs, _c, _ = imaging(fail="no software specification exists for the cluster")
        state, detail = vs.ensure_cluster_image("cluster-c", True)
        ck("image API: a missing software spec is the one case Setup Image is the answer",
           state == BLOCKED and "Setup Image" in detail)

        vs, calls, _ = imaging(flips=False)
        state, detail = vs.ensure_cluster_image("cluster-a", True)
        ck("image API: an enable that returns but changes nothing is not called success",
           state == BLOCKED and "still reports baseline" in detail)

        vs, calls, _ = imaging(check_fails=True)
        state, detail = vs.ensure_cluster_image("cluster-a", True)
        ck("image API: if the check cannot run, nothing one-way is attempted",
           state == BLOCKED and "PUT" not in [c[0] for c in calls])

        ck("image API: the task poller waits on /cis/tasks and surfaces a failure",
           _vapi_task_cases())
        ck("vapi_message: digs the human line out of a nested vAPI answer",
           vapi_message({"error": {"messages": [
               {"default_message": "Host is not stateful"}]}}) == "Host is not stateful")
        ck("vapi_message: and out of a 400 body, without the type signature",
           vapi_message(json.loads(
               '{"error_type":"UNEXPECTED_INPUT","messages":[{"args":["check$task"],'
               '"default_message":"Invalid input for method: check$task"},'
               '{"args":["checks_to_skip","LIST[STRING]"],"default_message":"Missing"}]}'))
           == "Invalid input for method: check$task; Missing")

        fmr = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fmr
        res = step_ready(cfg, dict(st), False)
        ck("step ready: a baseline-managed cluster blocks the import",
           any(s == BLOCKED and "baseline-managed" in d for s, d in res.rows))
        # A cluster that is not there yet is not baseline-managed, and must not be
        # sent to --only image, which converts an existing cluster and cannot make one.
        # ---- NTP. VCF checks it at import and at every inventory sync afterwards;
        # both of those failed on this engagement before the step existed.
        _ntp_want = ["10.0.0.10", "10.0.0.11"]
        ck("ntp: the host list falls back to the vCenter's time source",
           ntp_servers({"vcenter": {"ntp": _ntp_want}}) == _ntp_want)
        ck("ntp: and a site-level ntp: overrides it",
           ntp_servers({"ntp": ["10.0.0.1"], "vcenter": {"ntp": _ntp_want}}) == ["10.0.0.1"])

        class _NtpHost:
            """Only the bits of a HostSystem that ntp_state and ensure_ntp touch."""
            def __init__(self, servers, running, policy, firewall=True, has_svc=True):
                self.acted, self.started, self.restarted = [], False, False
                outer = self

                class _DTS:
                    dateTimeInfo = type("I", (), {"ntpConfig": type(
                        "N", (), {"server": list(servers)})()})()

                    def UpdateDateTimeConfig(self, spec):
                        outer.acted.append(("servers", list(spec.ntpConfig.server)))

                class _SS:
                    serviceInfo = type("SI", (), {"service": (
                        [type("S", (), {"key": "ntpd", "running": running,
                                        "policy": policy})()] if has_svc else [])})()

                    def UpdateServicePolicy(self, id, policy):
                        outer.acted.append(("policy", id, policy))

                    def StartService(self, id):
                        outer.started = True
                        outer.acted.append(("start", id))

                    def RestartService(self, id):
                        outer.restarted = True
                        outer.acted.append(("restart", id))

                class _FW:
                    firewallInfo = type("FI", (), {"ruleset": [
                        type("R", (), {"key": "ntpClient", "enabled": firewall})()]})()

                    def EnableRuleset(self, id):
                        outer.acted.append(("firewall", id))

                self.configManager = type("CM", (), {
                    "dateTimeSystem": _DTS(), "serviceSystem": _SS(),
                    "firewallSystem": _FW()})()

        class _NtpVS(_FakeVS):
            """_FakeVS with the real ntp_state and ensure_ntp over a scripted host."""
            ntp_state = VSphere.ntp_state
            ensure_ntp = VSphere.ensure_ntp

            def __init__(self, host):
                _FakeVS.__init__(self, present=["h1.example.com"])
                self.host = host

            def find(self, kind, name, root=None):
                return self.host

        # The state found in the field: servers right, daemon
        # stopped. The old script called this host ready and VCF refused the sync.
        h = _NtpHost(_ntp_want, running=False, policy="off")
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, True)
        ck("ntp: a stopped daemon with correct servers is started, not called ready",
           h.started and any(s == CHANGE and "started" in d for s, d in rows))
        ck("ntp: and its start policy is set to on, so a reboot does not undo it",
           ("policy", "ntpd", "on") in h.acted)
        ck("ntp: servers already right are not rewritten",
           not any(a[0] == "servers" for a in h.acted))

        h = _NtpHost([], running=True, policy="on")
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, True)
        ck("ntp: missing servers are set on a daemon that is already running",
           ("servers", _ntp_want) in h.acted)
        ck("ntp: and the daemon is restarted, or it keeps using the old list",
           h.restarted)

        # The quiet one: ntpd runs, the ruleset is shut, nothing ever reaches the
        # server. The services page shows "Running" and the host looks correct.
        h = _NtpHost(_ntp_want, running=True, policy="on", firewall=False)
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, True)
        ck("ntp: a closed ntpClient ruleset is opened, not read as a running daemon",
           ("firewall", "ntpClient") in h.acted)

        h = _NtpHost(_ntp_want, running=True, policy="on")
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, False)
        ck("ntp: a correct host is left alone and reports ok",
           h.acted == [] and all(s == OK for s, _d in rows))

        h = _NtpHost([], running=False, policy="off")
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, False)
        ck("ntp: a plan changes nothing on the host",
           h.acted == [] and not h.started)
        ck("ntp: and names every change it would make",
           len([1 for s, _d in rows if s == CHANGE]) == 3)

        # Read-back: a policy that silently does not take must not report success.
        class _Stubborn(_NtpHost):
            pass
        h = _Stubborn(_ntp_want, running=False, policy="off")
        h.configManager.serviceSystem.UpdateServicePolicy = lambda id, policy: None
        h.configManager.serviceSystem.StartService = lambda id: None
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, True)
        ck("ntp: a start that did not take is reported, not assumed",
           any(s == BLOCKED and "still reports" in d for s, d in rows))

        h = _NtpHost(_ntp_want, running=True, policy="on", has_svc=False)
        rows = _NtpVS(h).ensure_ntp("h1.example.com", _ntp_want, True)
        ck("ntp: a host with no ntpd service is refused rather than crashed on",
           any(s == BLOCKED and "no ntpd service" in d for s, d in rows))

        fnone = _FakeVS(present=cfg["hosts"])
        fnone.ntp = {"running": False, "policy": "off"}
        g["_connect"] = lambda c, s: fnone
        res = step_ready(cfg, dict(st), False)
        ck("step ready: a stopped ntpd blocks the import and names the host",
           any(s == BLOCKED and "NTP not right" in d and "ntpd stopped" in d
               for s, d in res.rows))
        ck("step ready: and points at --only ntp, the command that fixes it",
           any("--only ntp --commit" in d for _s, d in res.rows))

        fok = _FakeVS(present=cfg["hosts"])
        fok.ntp = {"servers": ntp_servers(cfg)}      # what this config actually asks for
        g["_connect"] = lambda c, s: fok
        res = step_ready(cfg, dict(st), False)
        ck("step ready: NTP in order reads ok rather than staying silent",
           any(s == OK and d.startswith("NTP ") for s, d in res.rows))
        fwrong = _FakeVS(present=cfg["hosts"])
        fwrong.ntp = {"servers": ["pool.ntp.org"]}   # running, but the wrong source
        g["_connect"] = lambda c, s: fwrong
        res = step_ready(cfg, dict(st), False)
        ck("step ready: a host pointed at the wrong time source is caught too",
           any(s == BLOCKED and "NTP not right" in d and "pool.ntp.org" in d
               for s, d in res.rows))

        fma = _FakeVS(present=cfg["hosts"])
        fma.no_cluster = True
        g["_connect"] = lambda c, s: fma
        res = step_ready(cfg, dict(st), False)
        ck("step ready: an absent cluster is reported as absent, not baseline-managed",
           any(s == BLOCKED and "does not exist yet, so it has no image" in d
               for s, d in res.rows))
        ck("step ready: and is not sent to the conversion that cannot create it",
           not any("--only image" in d for s, d in res.rows))
        fmi = _FakeVS(present=cfg["hosts"])
        fmi.imaged = True
        fmi.already = set(cfg["hosts"])
        g["_connect"] = lambda c, s: fmi
        res = step_ready(cfg, dict(st), False)
        ck("step ready: and passes once it is on an image",
           any(s == OK and "vLCM image" in d for s, d in res.rows))
        ck("step ready: a datastore every host can see is reported",
           any(s == OK and "a datastore every host can see" in d for s, d in res.rows))

        # vSAN switched off leaves the disks claimed and everything else looking
        # fine. Only the import's common-datastore check caught it, at the second site.
        fnd = _FakeVS(present=cfg["hosts"])
        fnd.imaged = True
        fnd.already = set(cfg["hosts"])
        fnd.no_datastore = True
        g["_connect"] = lambda c, s: fnd
        res = step_ready(cfg, dict(st), False)
        ck("step ready: no shared datastore blocks the import, as the wizard would",
           any(s == BLOCKED and "datastore" in d for s, d in res.rows))
        ck("step ready: and points at vSAN, the way it actually failed",
           any("--only vsan" in d for _s, d in res.rows))

        # A host that is not in the vCenter cannot be read, and must not be counted
        # compliant: cluster-d reported "0 of 8 hosts in the vCenter" and "8 of 8 on
        # a dedicated portgroup" in the same table.
        fnew = _FakeVS(present=[])
        g["_connect"] = lambda c, s: fnew
        res = step_ready(cfg, dict(st), False)
        ck("step ready: hosts not yet in the vCenter are not counted as compliant",
           not any("%d of %d hosts" % (len(cfg["hosts"]), len(cfg["hosts"])) in d
                   for _s, d in res.rows))
        ck("step ready: and it says plainly that it cannot tell",
           any(s == BLOCKED and "cannot tell" in d for s, d in res.rows))
        fsome = _FakeVS(present=cfg["hosts"][:1])
        fsome.already = set(cfg["hosts"][:1])
        g["_connect"] = lambda c, s: fsome
        res = step_ready(cfg, dict(st), False)
        ck("step ready: a partial inventory reports what it checked, not what it hoped",
           any("not in the vCenter yet, so unchecked" in d for _s, d in res.rows))

        fmr = _FakeVS(present=cfg["hosts"])
        g["_connect"] = lambda c, s: fmr
        res = step_ready(cfg, dict(st), False)
        ck("step ready: vmk0 still on a vSwitch blocks the import",
           res.state == BLOCKED and any("dedicated portgroup" in d for _, d in res.rows))
        fmr2 = _FakeVS(present=cfg["hosts"])
        fmr2.already = set(cfg["hosts"])
        g["_connect"] = lambda c, s: fmr2
        res = step_ready(cfg, dict(st), False)
        ck("step ready: once vmk0 has moved the check passes",
           not any("dedicated portgroup" in d for s_, d in res.rows if s_ == BLOCKED))

        fake4 = _FakeVS(present=["esx01.example.com"])
        g["_connect"] = lambda c, s: fake4
        res = step_ready(cfg, dict(st), False)
        ck("step ready: a host missing from the vCenter blocks", res.state == BLOCKED)

        fake5 = _FakeVS(present=cfg["hosts"] + ["stray.example.com"])
        g["_connect"] = lambda c, s: fake5
        res = step_ready(cfg, dict(st), False)
        ck("step ready: an extra host is called out, since import takes every cluster",
           any("stray.example.com" in d for _, d in res.rows))

        g["soap_about"] = lambda h, timeout=8: (None, None)
        res = step_objects(cfg, dict(st), False)
        g["_connect"] = saved["_connect"]
        ck("steps block cleanly when the vCenter is not up",
           step_hosts(cfg, dict(st), False).state == BLOCKED)
    finally:
        g.update(saved)

    # HA ordering: vSphere refuses vSAN configuration while HA is on, so the cluster
    # is born with HA off and step_vsan turns it on after the disks are claimed.
    try:
        from pyVmomi import vim as _vim
        _vs = VSphere.__new__(VSphere)
        _vs.vim = _vim
        ck("cluster spec: HA off leaves dasConfig disabled",
           _vs.cluster_spec(False, True).dasConfig.enabled is False)
        ck("cluster spec: HA on carries a complete das block",
           all(getattr(_vs.cluster_spec(True, True).dasConfig, f, None) is not None
               for f in ("hostMonitoring", "admissionControlPolicy", "defaultVmSettings",
                         "failoverLevel")))
        ck("cluster spec: DRS carried through", _vs.cluster_spec(True, True).drsConfig.enabled is True)
    except ImportError:
        pass

    fake6 = _FakeVS(present=cfg["hosts"])
    g["_connect"] = lambda c, s: fake6
    step_vsan(cfg, dict(st), True)
    ck("vsan: HA is turned on by the vsan step, not at cluster creation",
       any(m.startswith("vsan") for m in fake6.mutations)
       and any(m.startswith("ha on") for m in fake6.mutations))
    g["_connect"] = saved["_connect"]

    # Disk claiming is destructive, so the grouping is pinned here.
    try:
        from pyVmomi import vim as _v                                    # noqa: F401
        _vs = VSphere.__new__(VSphere)

        def _disk(name, gb):
            return type("D", (), {"canonicalName": name,
                                  "capacity": type("C", (), {"block": gb * 1953125,
                                                             "blockSize": 512})()})()
        six = [_disk("naa.%d" % i, 960) for i in range(6)]
        two = _vs.disk_group_plan(six, 2)
        flat = [d for c, caps in two for d in [c] + caps]
        ck("disks: 2 groups from 6 is 1 cache + 2 capacity each",
           len(two) == 2 and all(len(caps) == 2 for _, caps in two))

        def _d(model, gb):
            return type("D", (), {"canonicalName": "naa." + model, "model": model,
                                  "capacity": type("C", (), {"block": int(gb * 1953125),
                                                             "blockSize": 512})()})()
        mixed6 = [_d("cacheclass", 1920)] + [_d("capclass", 3840) for _ in range(5)]
        auto = _vs.disk_group_plan(mixed6, "auto")
        ck("disks: auto reads one cache-class disk as one group",
           len(auto) == 1 and len(auto[0][1]) == 5)
        ck("disks: auto never drafts a capacity drive into cache",
           all(c.model == "cacheclass" for c, _ in auto))
        ck("disks: auto reads two cache-class disks as two groups",
           len(_vs.disk_group_plan([_d("cacheclass", 1920)] * 2
                                   + [_d("capclass", 3840)] * 4, "auto")) == 2)
        ck("disks: auto on one size class is a single group",
           len(_vs.disk_group_plan([_d("same", 960) for _ in range(6)], "auto")) == 1)

        # All-flash claiming: an SSD in a capacity slot is refused by the host
        # ("is an SSD and can not be used as a vSAN HDD"), so the claim goes through
        # the vSAN endpoint, which is told which kind of cluster it is building.
        def _scsi(model, gb, ssd=True):
            return _v.host.ScsiDisk(
                deviceName="/vmfs/devices/disks/naa.%s" % model, deviceType="disk",
                key="k-%s" % model, uuid="u-%s" % model, canonicalName="naa.%s" % model,
                model=model, ssd=ssd,
                capacity=_v.host.DiskDimensions.Lba(block=int(gb * 1953125), blockSize=512))

        def _spec(disks):
            pl = _vs.disk_group_plan(disks, "auto")
            return _v.vsan.host.DiskMappingCreationSpec(
                host=_v.HostSystem("host-1", None),
                cacheDisks=[c for c, _ in pl],
                capacityDisks=[d for _, caps in pl for d in caps],
                creationType="allFlash" if all(d.ssd for d in disks) else "hybrid")

        af = _spec([_scsi("cacheclass", 1920)] + [_scsi("cap%d" % i, 3840) for i in range(5)])
        ck("claim: all-SSD disks are claimed as an all-flash cluster",
           af.creationType == "allFlash")
        ck("claim: cache and capacity go in their own fields, every disk placed once",
           len(af.cacheDisks) == 1 and len(af.capacityDisks) == 5)
        hy = _spec([_scsi("ssd", 400)] + [_scsi("hdd%d" % i, 4000, ssd=False) for i in range(5)])
        ck("claim: spinning capacity is claimed as hybrid", hy.creationType == "hybrid")
        ck("disks: every disk used exactly once, none invented",
           len(flat) == 6 and len({id(d) for d in flat}) == 6)
        ck("disks: 1 group from 6 is 1 cache + 5 capacity",
           len(_vs.disk_group_plan(six, 1)[0][1]) == 5)
        ck("disks: an odd count still places every disk",
           len([d for c, caps in _vs.disk_group_plan(six[:5], 2) for d in [c] + caps]) == 5)
        ck("disks: the smallest disks are chosen as cache",
           all(c.canonicalName.startswith("cache") for c, _ in _vs.disk_group_plan(
               [_disk("cache0", 400), _disk("cap0", 1920),
                _disk("cap1", 1920), _disk("cache1", 400)], 2)))
        ck("disks: more groups than disks allow is capped, never empty",
           len(_vs.disk_group_plan(six, 99)) == 3
           and all(caps for _, caps in _vs.disk_group_plan(six, 99)))
    except ImportError:
        pass

    # The appliance upgrade: version compare, ordering, and the three secrets.
    ck("version: 8.0.3 is below 9.1", version_at_least("8.0.3", "9.1") is False)
    ck("version: 9.1.1 is at or past 9.1", version_at_least("9.1.1", "9.1") is True)
    ck("version: 9.1 is below 9.1.1", version_at_least("9.1", "9.1.1") is False)
    ck("version: a build suffix does not confuse it",
       version_at_least("8.0.3.00000-24022515", "9.1") is False)
    ck("upgrade runs after the appliance exists and before anything uses it",
       STEPS.index("upgrade") == STEPS.index("vcsa") + 1
       and STEPS.index("upgrade") < STEPS.index("objects"))

    # no template on the ISO: the spec is built whole, from what this tool knows
    up = vcsa_upgrade_spec(cfg, None, "10.0.0.99", "pg-read-off-the-host", "vc8-91",
                           "HOSTPW", "ROOTPW", "SSOPW")
    blob = json.dumps(up)
    ck("upgrade spec: deployment network is the one passed in, not the configured one",
       up["new_vcsa"]["esxi"]["deployment_network"] == "pg-read-off-the-host"
       and cfg["vcenter"]["target"]["network"] not in blob)

    # ---- the deployment network is read, never taken on trust
    class _VM:
        def __init__(self, name, ips=(), nets=(), hostname=None):
            self.name = name
            self.guest = type("G", (), {"ipAddress": (ips[0] if ips else None),
                                        "hostName": hostname,
                                        "net": [type("N", (), {"ipAddress": list(ips)})()]})()
            self.network = [type("P", (), {"name": n})() for n in nets]

    def _esx(vms, boom=False):
        class _C:
            def __init__(self, *a):
                if boom:
                    raise RuntimeError("Cannot complete login")
                self.vim = type("V", (), {"VirtualMachine": object})
            def _all(self, kind):
                return vms
        return _C

    real_vs = g["VSphere"]
    g["VSphere"] = _esx([_VM("other", ["10.0.0.9"], ["pg-other"]),
                         _VM("vc8", ["10.0.0.60"], ["pg-vm-mgmt"])])
    ck("read appliance: found by the address the appliance answers on",
       read_appliance("esx1", "pw", "10.0.0.60", "vc8.example.com")
       == ("pg-vm-mgmt", {"other", "vc8"}, None))
    ck("read appliance: every VM name on the host comes back in the same login",
       read_appliance("esx1", "pw", "10.0.0.60", "vc8.example.com")[1] == {"other", "vc8"})

    g["VSphere"] = _esx([_VM("vc8", [], ["pg-vm-mgmt"], hostname="vc8.example.com")])
    ck("read appliance: found by name when the guest reports no address",
       read_appliance("esx1", "pw", "10.0.0.60", "vc8.example.com")[0] == "pg-vm-mgmt")

    g["VSphere"] = _esx([_VM("somethingelse", ["10.0.0.9"], ["pg-other"])])
    net_, taken_, why_ = read_appliance("esx1", "pw", "10.0.0.60", "vc8.example.com")
    ck("read appliance: a missing appliance is a reason, never a guess",
       net_ is None and "another host" in why_)

    g["VSphere"] = _esx([_VM("vc8", ["10.0.0.60"], ["pg-a", "pg-b"])])
    net_, taken_, why_ = read_appliance("esx1", "pw", "10.0.0.60", "vc8.example.com")
    ck("read appliance: two networks is ambiguous, so it asks rather than picks",
       net_ is None and "pg-a, pg-b" in why_)

    g["VSphere"] = _esx([], boom=True)
    net_, taken_, why_ = read_appliance("esx1", "pw", "10.0.0.60", "vc8.example.com")
    ck("read appliance: a login failure is reported in one line",
       net_ is None and taken_ == set() and "cannot log in" in why_)

    # both appliances exist at once during the migration, so the name has to differ
    ck("free name: the appliance being replaced does not lend its name to the new one",
       free_vm_name("vc01", "9.1", {"vc01"}) == "vc01-91")
    ck("free name: an unused name is left alone",
       free_vm_name("vc01", "9.1", {"something"}) == "vc01")
    ck("free name: a leftover from a previous attempt is stepped over",
       free_vm_name("vc01", "9.1", {"vc01", "vc01-91"}) == "vc01-91-2")
    ck("free name: a target with no digits still yields a name",
       free_vm_name("vc", "latest", {"vc"}) == "vc-new")

    upcfg = json.loads(json.dumps(cfg))
    upcfg["upgrade"] = {"target": "9.1", "installer": __file__}
    upcfg["vcenter"]["temp_ip"] = "10.0.0.99"
    stb = dict(st)
    stb["precheck"] = True
    stb["pw"] = {"vcenter.target.password": "pw"}
    stb["outdir"] = os.environ.get("TMPDIR", "/tmp")
    real_about, real_sub = g["soap_about"], g["subprocess"]
    g["soap_about"] = lambda f, timeout=5: ("8.0.3", "24022515")
    g["subprocess"] = type("S", (), {"call": staticmethod(lambda *a, **k: 0)})
    try:
        g["VSphere"] = _esx([_VM("somethingelse", ["10.0.0.9"], ["pg-other"])])
        res = step_upgrade(upcfg, dict(stb), False)
        ck("upgrade: an unreadable deployment network blocks before the spec is written",
           res.state == BLOCKED and any("will not guess" in d for _, d in res.rows)
           and not os.path.exists(os.path.join(stb["outdir"], "vcsa-upgrade.json")))

        g["VSphere"] = _esx([_VM("vc8", ["10.0.0.60"], ["pg-vm-mgmt"])])
        res = step_upgrade(upcfg, dict(stb), False)
        ck("upgrade: a stale vcenter.target.network is named and overridden, not obeyed",
           any("stale and was ignored" in d and "pg-vm-mgmt" in d for _, d in res.rows))

        agree = json.loads(json.dumps(upcfg))
        agree["vcenter"]["target"]["network"] = "pg-vm-mgmt"
        res = step_upgrade(agree, dict(stb), False)
        ck("upgrade: when the config agrees it is simply confirmed",
           any(s == OK and "read off" in d for s, d in res.rows)
           and not any("stale" in d for _, d in res.rows))

        res = step_upgrade(upcfg, dict(st), False)
        ck("upgrade: a plan says the network will be read, and reads nothing yet",
           res.state == CHANGE and any("read off" in d and "at commit" in d for _, d in res.rows))

        # the stall this fixes: the source appliance still holds the name all migration
        g["VSphere"] = _esx([_VM("vc8", ["10.0.0.60"], ["pg-vm-mgmt"])])
        res = step_upgrade(upcfg, dict(stb), False)
        ck("upgrade: the new appliance is given a name the host does not already have",
           any("named vc8-91" in d and "vc8 is still the one being replaced" in d
               for _, d in res.rows))

        named = json.loads(json.dumps(upcfg))
        named["vcenter"]["target"]["appliance_name"] = "vc8-chosen"
        res = step_upgrade(named, dict(stb), False)
        ck("upgrade: a configured appliance name wins over the derived one",
           any("named vc8-chosen" in d for _, d in res.rows))

        # ---- --precheck must touch nothing at all
        real_ping, real_dns = g["ip_free"], g["dns_check"]
        g["ip_free"] = lambda ip, timeout=2: True
        g["dns_check"] = lambda f, ip=None: (True, "%s resolves to %s" % (f, ip))
        calls = []
        g["subprocess"] = type("S", (), {
            "call": staticmethod(lambda *a, **k: calls.append(a) or 0),
            "DEVNULL": -3})
        try:
            spec_file = os.path.join(stb["outdir"], "vcsa-upgrade.json")
            if os.path.exists(spec_file):
                os.remove(spec_file)
            res = step_upgrade(upcfg, dict(stb), False)
            ck("precheck: the installer is never run and no spec is left on disk",
               calls == [] and not os.path.exists(spec_file))
            ck("precheck: it says plainly that nothing was changed",
               any("nothing was changed" in d for _, d in res.rows))
            ck("precheck: a clean check still counts as work pending, not a finished run",
               res.state == CHANGE)
            ck("precheck: the redacted spec is there to read",
               any("passwords removed" in d for _, d in res.rows))

            stv = dict(stb)
            stv["vendor_precheck"] = True
            res = step_upgrade(upcfg, stv, False)
            ck("vendor precheck: refused without --commit, because it changes the appliance",
               res.state == BLOCKED and calls == []
               and any("needs --commit" in d and "VLCM" in d for _, d in res.rows))

            res = step_upgrade(upcfg, stv, True)
            ck("vendor precheck: with --commit it runs the installer and says what it changed",
               any("--precheck-only" in " ".join(a[0]) for a in calls)
               and any("that change is being made now" in d for _, d in res.rows))

            busy = dict(stb)
            g["ip_free"] = lambda ip, timeout=2: False
            res = step_upgrade(upcfg, busy, False)
            ck("precheck: a temporary address that answers blocks the upgrade",
               res.state == BLOCKED and any("answers to ping" in d for _, d in res.rows))
            g["ip_free"] = lambda ip, timeout=2: True

            # The installer polls the appliance it is upgrading; losing that poll is not
            # the upgrade failing. Before the run the appliance is on 8.0.3, after it is
            # whatever the second reading says - so the stub answers in sequence.
            def _versions(*seq):
                box = {"n": 0}

                def about(f, timeout=5):
                    v = seq[min(box["n"], len(seq) - 1)]
                    box["n"] += 1
                    return v if isinstance(v, tuple) else (v, "b")
                return about

            stc = dict(stb)
            stc["precheck"] = False
            g["subprocess"] = type("S", (), {"call": staticmethod(lambda *a, **k: 1),
                                             "DEVNULL": -3})

            g["soap_about"] = _versions("8.0.3", ("9.1.1", "25712839"))
            res = step_upgrade(upcfg, dict(stc), True)
            ck("upgrade: a lost connection is not a failed upgrade when the appliance "
               "reached the target",
               res.state != BLOCKED
               and any(s == ADD and "lost its connection" in d for s, d in res.rows))
            ck("upgrade: and it still says to verify, because nothing confirmed the tail end",
               any("post-upgrade tasks were never confirmed" in d for _, d in res.rows))

            g["soap_about"] = _versions("8.0.3", "8.0.3")
            res = step_upgrade(upcfg, dict(stc), True)
            ck("upgrade: an installer failure with the appliance still on the old version "
               "stays blocked",
               res.state == BLOCKED and any("still 8.0.3" in d for _, d in res.rows))

            g["soap_about"] = _versions("8.0.3", (None, None))
            res = step_upgrade(upcfg, dict(stc), True)
            ck("upgrade: an installer failure with nothing answering stays blocked",
               res.state == BLOCKED and any("not answering" in d for _, d in res.rows))
            g["soap_about"] = lambda f, timeout=5: ("8.0.3", "24022515")
        finally:
            g["ip_free"], g["dns_check"] = real_ping, real_dns

    finally:
        g["soap_about"], g["subprocess"], g["VSphere"] = real_about, real_sub, real_vs

    # two migrations onto one host is what costs half an hour and a revert
    _lockdir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "deploy_vc_locktest")
    _first, _who = take_host_lock("esx1.example.com", _lockdir, "site-a")
    if _first is None and _who is None:
        ck("host lock: not available on this platform, skipped", True)
    else:
        _second, _who2 = take_host_lock("esx1.example.com", _lockdir, "site-b")
        ck("host lock: a second deployment onto the same host is refused",
           _second is None and "site-a" in (_who2 or ""))
        _other, _ = take_host_lock("esx2.example.com", _lockdir, "site-c")
        ck("host lock: a different host is unaffected", _other is not None)
        if _other:
            _other.close()
        _first.close()
        _again, _ = take_host_lock("esx1.example.com", _lockdir, "site-d")
        ck("host lock: released when the run that held it is done", _again is not None)
        if _again:
            _again.close()

    try:
        from pyVmomi import vmodl as _vmodl
    except ImportError:
        ck("fault: pyvmomi absent, InvalidArgument check skipped", True)
    else:
        # "A specified parameter was not correct:" with nothing after the colon is
        # what this looked like before, and it is useless
        _ia = _vmodl.fault.InvalidArgument(invalidProperty="spec.backing.pnicSpec")
        ck("fault: InvalidArgument names the parameter it objected to",
           "spec.backing.pnicSpec" in fault(_ia))
        ck("fault: and does not leave a dangling colon",
           not fault(_ia).rstrip().endswith(":"))

        # The wrapper said "one or more host members" and wait() then threw the rest
        # away, so the reason never reached the evidence file.
        from pyVmomi import vim as _v
        _inner = _vmodl.fault.InvalidArgument(invalidProperty="spec.host.backing")
        _bulk = _v.fault.DvsOperationBulkFault(
            msg="Cannot complete a vSphere Distributed Switch operation for one or more "
                "host members.",
            hostFault=[_v.fault.DvsOperationBulkFault.FaultOnHost(fault=_inner)])
        ck("fault: a bulk DVS fault reports the host's reason, not the wrapper",
           "spec.host.backing" in fault(_bulk)
           and "one or more host members" not in fault(_bulk))
        ck("fault: a task error is unwrapped to the fault it carried",
           "spec.host.backing" in fault(TaskError(_bulk)))
        ck("fault: and a task error still reads as its message",
           "one or more host members" in str(TaskError(_bulk)))

    ck("spec problems: a value the template never let us set is caught",
       any("deployment_network" in x for x in spec_problems(
           {"new_vcsa": {"esxi": {}}}, {"new_vcsa.esxi.deployment_network": "pg-a"})))
    ck("spec problems: a template placeholder left behind is caught",
       any("placeholder" in x or "still the template" in x for x in spec_problems(
           {"new_vcsa": {"esxi": {"datastore": "<datastore>"}}}, {})))
    ck("spec problems: an empty password is caught",
       any("is empty" in x for x in spec_problems(
           {"source_vc": {"vc_vcsa": {"root_password": ""}}}, {})))
    ck("spec problems: a sound spec reports nothing",
       spec_problems({"new_vcsa": {"esxi": {"password": "x", "deployment_network": "pg-a"}}},
                     {"new_vcsa.esxi.deployment_network": "pg-a"}) == [])
    ck("upgrade spec: new appliance goes to the temporary address",
       up["new_vcsa"]["temporary_network"]["ip"] == "10.0.0.99")
    ck("upgrade spec: source is the appliance being replaced",
       up["source_vc"]["vc_vcsa"]["hostname"] == cfg["vcenter"]["fqdn"])
    ck("upgrade spec: each secret only where it belongs",
       up["new_vcsa"]["esxi"]["password"] == "HOSTPW"
       and up["source_vc"]["managing_esxi_or_vc"]["password"] == "HOSTPW"
       and up["source_vc"]["vc_vcsa"]["root_password"] == "ROOTPW"
       and up["source_vc"]["vc_vcsa"]["password"] == "SSOPW"
       and blob.count("HOSTPW") == 2 and blob.count("ROOTPW") == 1
       and blob.count("SSOPW") == 1)
    ck("upgrade spec: new_vcsa.os carries no password - the upgrade inherits the source's",
       "password" not in up["new_vcsa"]["os"] and up["new_vcsa"]["os"]["ssh_enable"] is True)

    # The schema rejects keys it does not know and names one per run, so with a template
    # in hand the spec follows the template's shape instead of guessing at the schema.
    tpl = {"__version": "2.14.0", "__comments": "x",
           "new_vcsa": {"os": {"ssh_enable": False, "keep_me": "x"},
                        "esxi": {"hostname": "", "password": "", "port": 443},
                        "appliance": {"name": ""},
                        "temporary_network": {"ip": ""},
                        "odd_section": {"a": 1}},
           "source_vc": {"vc_vcsa": {"hostname": "", "password": "", "port": 443}}}
    merged = vcsa_upgrade_spec(cfg, tpl, "10.0.0.99", "pg", "vc8-91", "H", "R", "S")
    ck("upgrade spec: keeps the template's __version", merged["__version"] == "2.14.0")
    ck("upgrade spec: values land on the keys the template declares",
       merged["new_vcsa"]["esxi"]["password"] == "H"
       and merged["new_vcsa"]["os"]["ssh_enable"] is True
       and merged["new_vcsa"]["temporary_network"]["ip"] == "10.0.0.99"
       and merged["source_vc"]["vc_vcsa"]["password"] == "S")
    ck("upgrade spec: keys the template does not declare are never added",
       "deployment_network" not in merged["new_vcsa"]["esxi"]
       and "password" not in merged["new_vcsa"]["os"]
       and "root_password" not in merged["source_vc"]["vc_vcsa"])
    ck("upgrade spec: sections the template does not have are not invented",
       "user_options" not in merged["new_vcsa"]
       and "managing_esxi_or_vc" not in merged["source_vc"]
       and "ceip" not in merged)
    ck("upgrade spec: the template's own keys and sections survive",
       merged["new_vcsa"]["os"]["keep_me"] == "x"
       and merged["new_vcsa"]["esxi"]["port"] == 443
       and merged["new_vcsa"]["odd_section"] == {"a": 1})
    ck("upgrade spec: the template on disk is left alone",
       tpl["new_vcsa"]["os"] == {"ssh_enable": False, "keep_me": "x"}
       and tpl["new_vcsa"]["esxi"]["password"] == "")
    ck("upgrade spec: with no template at all, everything is set",
       "deployment_network" in up["new_vcsa"]["esxi"] and "user_options" in up["new_vcsa"])

    miss = []
    vcsa_upgrade_spec(cfg, tpl, "10.0.0.99", "pg", "vc8-91", "H", "R", "S", miss)
    ck("upgrade spec: whatever the template omits is reported, never dropped quietly",
       "esxi.deployment_network" in miss and "os.password" not in miss
       and "user_options" in miss and "ceip" in miss)
    ck("upgrade spec: nothing is reported for keys the template does declare",
       "esxi.password" not in miss and "temporary_network.ip" not in miss)

    red = redact(merged, {"H", "R", "S"})
    ck("redact: every secret is masked",
       red["new_vcsa"]["esxi"]["password"] == "********"
       and red["source_vc"]["vc_vcsa"]["password"] == "********")
    ck("redact: everything else survives for reading",
       red["new_vcsa"]["temporary_network"]["ip"] == "10.0.0.99"
       and red["new_vcsa"]["esxi"]["port"] == 443)

    st_up = dict(st)
    ck("upgrade: no target means the appliance is left alone",
       step_upgrade(cfg, st_up, False).rows[0][0] == SKIP)
    withtgt = dict(cfg)
    withtgt["upgrade"] = {"target": "9.1", "installer": None}
    g["soap_about"] = lambda h, timeout=8: ("9.1.1", "25691509")
    ck("upgrade: already at the target is a no-op",
       step_upgrade(withtgt, dict(st), False).rows[0][0] == OK)
    g["soap_about"] = lambda h, timeout=8: ("8.0.3", "24022515")
    ck("upgrade: below the target with no installer blocks, and says why",
       step_upgrade(withtgt, dict(st), False).state == BLOCKED)
    g["soap_about"] = saved["soap_about"]

    ck("every step has a function", sorted(STEP_FUNCS) == sorted(STEPS))

    # --- the config layer, as a multi-site config file does it
    sites_cfg = parse_yaml(SITES_EXAMPLE)
    ck("sites: the first one is the default", pick_site(sites_cfg, None) == "site-a")
    ck("sites: --site picks the other", pick_site(sites_cfg, "site-b") == "site-b")
    merged = site_config(sites_cfg, "site-a")
    ck("sites: the site's own values come through", merged["cluster"]["name"] == "cluster01")
    ck("sites: shared values fold in", merged["esx"]["build"] == "12345678")
    ck("sites: hosts belong to the site", merged["hosts"][0] == "esx01.example.com")
    ck("sites: the other site is not mixed in",
       site_config(sites_cfg, "site-b")["cluster"]["name"] == "cluster02")

    ck("keys: a good config passes", check_keys(sites_cfg, KNOWN) == [])

    ck("unset: blank", unset(None) and unset(""))
    ck("unset: a [set] placeholder, however YAML reads it", unset("[set]") and unset(["set"]))
    ck("unset: a [verify] placeholder", unset("[verify]") and unset(["verify"]))
    ck("unset: a real value is not unset", not unset("10.0.0.254") and not unset(["10.0.0.10"]))
    ck("unset: a filled list is not unset", not unset([10, 20]))
    typo = parse_yaml(SITES_EXAMPLE.replace("  datacenter: dc01", "  datacentre: dc01"))
    bad = check_keys(typo, KNOWN)
    ck("keys: a typo stops the run and names the nearest",
       len(bad) == 1 and bad[0][0].endswith("datacentre") and bad[0][1] == "datacenter")
    extra = parse_yaml(SITES_EXAMPLE + "\n    extra:\n      anything: here\n")
    ck("keys: extra: holds raw keys, as fleetparity does", check_keys(extra, KNOWN) == [])

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(SITES_EXAMPLE)
        tmp = fh.name
    before = open(tmp).read().splitlines()
    backup = set_value(tmp, "sites.site-a.vcenter.vc_password", "'s3cret'")
    after = open(tmp).read().splitlines()
    reread = load_config(tmp)
    ck("--set: the value reads back",
       reread["sites"]["site-a"]["vcenter"]["vc_password"] == "s3cret")
    ck("--set: exactly one line changed",
       sum(1 for a_, b_ in zip(before, after) if a_ != b_) == 1 and len(before) == len(after))
    ck("--set: the comment on that line survives", "Blank: asked for" in
       [l for l in after if "vc_password" in l][0])
    ck("--set: the file before it is kept", os.path.exists(backup))
    ck("--set: the file is 600", oct(os.stat(tmp).st_mode)[-3:] == "600")
    os.unlink(tmp)
    os.unlink(backup)

    sec = Secrets({"vcenter": {"vc_password": "fromconfig"}}, "site-a", lambda k: k)
    ck("secrets: a password in the config is used, not asked for",
       sec.get("vcenter.vc_password") == "fromconfig")

    ck("exit code: blocked is 1", exit_code(1, 0, False, False) == 1)
    ck("exit code: a clean plan is 0", exit_code(0, 0, False, True) == 0)
    ck("exit code: pending with --detailed-exitcode is 2", exit_code(0, 2, False, True) == 2)
    ck("exit code: pending without the flag is 0", exit_code(0, 2, False, False) == 0)
    ck("exit code: a commit never returns 2", exit_code(0, 2, True, True) == 0)

    # ---- the real migration, against real pyVmomi types.
    # _FakeVS replaces migrate_mgmt_vmk wholesale, so without this the one call that can
    # strand a host is never actually built.
    try:
        from pyVmomi import vim as _vim
    except ImportError:
        ck("mgmt: pyvmomi absent, live migration checks skipped", True)
    else:
        class _O:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        def _host(on_vds=False, src_vlan=130, pnics=("vmnic2",), rollback=False,
                  uplinks=("vmnic3", "vmnic2")):
            seen = {}
            # a real spec, because the move now carries the host's own one through
            spec = _vim.host.VirtualNic.Specification(
                ip=_vim.host.IpConfig(dhcp=False, ipAddress="10.0.0.5",
                                      subnetMask="255.255.255.0"),
                mac="00:50:56:aa:bb:cc", mtu=1500, portgroup="Management Network",
                netStackInstanceKey="defaultTcpipStack")
            if on_vds:
                spec.distributedVirtualPort = _vim.dvs.PortConnection(
                    switchUuid="u1", portgroupKey="k1")
            vnic = _O(device="vmk0", portgroup="Management Network", spec=spec)
            info = _O(vnic=[vnic],
                      portgroup=[_O(spec=_O(name="Management Network",
                                            vswitchName="vSwitch0", vlanId=src_vlan))],
                      vswitch=[_O(name="vSwitch0",
                                  pnic=["key-vim.host.PhysicalNic-%s" % p for p in pnics],
                                  spec=_vim.host.VirtualSwitch.Specification(
                                      numPorts=128, mtu=1500,
                                      bridge=_vim.host.VirtualSwitch.BondBridge(
                                          nicDevice=list(pnics), beacon=None),
                                      policy=_vim.host.NetworkPolicy(
                                          nicTeaming=_vim.host.NetworkPolicy.NicTeamingPolicy(
                                              nicOrder=_vim.host.NetworkPolicy.NicOrderPolicy(
                                                  activeNic=list(pnics), standbyNic=[])))))],
                      proxySwitch=[_O(dvsUuid="u1", spec=_O(backing=_O(
                          pnicSpec=[_vim.dvs.HostMember.PnicSpec(pnicDevice=u)
                                    for u in uplinks])))])

            def update(cfg, mode):
                seen["cfg"], seen["mode"] = cfg, mode
                if not rollback:            # a real host leaves the vmkernel on the new port
                    vnic.spec.distributedVirtualPort = _vim.dvs.PortConnection(
                        switchUuid="u1", portgroupKey="k1")

            ns = _O(networkInfo=info, UpdateNetworkConfig=update)
            return _O(name="esx01.example.com", configManager=_O(networkSystem=ns)), seen

        def _dvs_(h, vlan=130, member=True, trunk=False):
            spec = (_vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec() if trunk
                    else _vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(vlanId=vlan))
            return _O(name="vds01", uuid="u1",
                      portgroup=[_O(name="pg-esx-mgmt", key="k1",
                                    config=_O(defaultPortConfig=_O(vlan=spec)))],
                      config=_O(host=[_O(config=_O(host=h))] if member else [],
                                configVersion="7"))

        def _vs(h):
            o = VSphere.__new__(VSphere)
            o.vim = _vim
            o.find = lambda kind, name, root=None: h if name == h.name else None
            return o

        def _move(h, dvs, pg="pg-esx-mgmt", allow=False, apply_=True):
            return _vs(h).migrate_mgmt_vmk(h.name, dvs, pg, "vmk0", allow, apply_)

        h, seen = _host()
        s, d = _move(h, _dvs_(h), apply_=False)
        ck("mgmt live: a plan touches nothing on the host", s == ADD and seen == {})
        ck("mgmt live: the plan names the vmkernel, the uplink and the VLAN",
           "vmk0" in d and "vmnic2" in d and "VLAN 130" in d)

        h, seen = _host()
        s, d = _move(h, _dvs_(h))
        cfgsent = seen.get("cfg")
        ck("mgmt live: the move succeeds", s == ADD)
        ck("mgmt live: one HostNetworkConfig, applied as modify",
           isinstance(cfgsent, _vim.host.NetworkConfig) and seen["mode"] == "modify")
        ck("mgmt live: the vmkernel moves on its own, with no uplink change beside it",
           len(cfgsent.vnic) == 1 and not cfgsent.proxySwitch)
        ck("mgmt live: the vmkernel is edited onto the target portgroup",
           cfgsent.vnic[0].device == "vmk0" and cfgsent.vnic[0].changeOperation == "edit"
           and cfgsent.vnic[0].spec.distributedVirtualPort.portgroupKey == "k1")
        # an edit replaces the spec, so everything the host had has to come back with it
        ck("mgmt live: the address, mac, mtu and stack are carried through, not dropped",
           cfgsent.vnic[0].spec.ip.ipAddress == "10.0.0.5"
           and cfgsent.vnic[0].spec.mac == "00:50:56:aa:bb:cc"
           and cfgsent.vnic[0].spec.mtu == 1500
           and cfgsent.vnic[0].spec.netStackInstanceKey == "defaultTcpipStack")
        ck("mgmt live: and the standard portgroup is cleared",
           cfgsent.vnic[0].spec.portgroup is None)

        # the uplink is a second, separate call - it cannot strand a vmkernel that is
        # already up on another nic, and if it fails the host is merely short of one
        h2, seen2 = _host(uplinks=("vmnic3",))
        s2, d2 = _move(h2, _dvs_(h2))
        ck("mgmt live: a failure adding the uplink still leaves the host up, and says so",
           s2 == CHANGE and "is on" in d2 and "one nic short of redundancy" in d2)
        ck("mgmt live: and the vmkernel move had already gone through first",
           seen2.get("cfg") is not None and not seen2["cfg"].proxySwitch)

        h, seen = _host(on_vds=True)
        s, _ = _move(h, _dvs_(h))
        ck("mgmt live: a host already migrated is not moved again", s == OK and seen == {})

        # a re-run has to finish the uplink, or a host that lost step two stays on one
        # nic for good
        h, seen = _host(on_vds=True, uplinks=("vmnic3",))
        s, d = _move(h, _dvs_(h))
        ck("mgmt live: a re-run still goes after the uplink it never managed to add",
           s == CHANGE and "one nic short of redundancy" in d and seen == {})

        # the nic needs a named free uplink port; a switch with none cannot take it
        class _VS(VSphere):
            ports = []

            def __init__(self):
                self.vim = _vim

            def free_uplinks(self, dvs, host, want):
                return self.ports[:want]

        hu, _ = _host(on_vds=True, uplinks=("vmnic3",))
        vs_ = _VS()
        vs_.find = lambda kind, name, root=None: hu
        s, d = vs_.add_uplink(hu, _dvs_(hu), hu.name, "vmk0", "pg-esx-mgmt",
                              ["vmnic2"], True)
        ck("mgmt live: no free uplink port is reported, not sent anyway",
           s == CHANGE and "only 0 free uplink port" in d)
        was = [_vim.dvs.HostMember.PnicSpec(pnicDevice="vmnic3", uplinkPortKey="32",
                                           uplinkPortgroupKey="dvportgroup-23",
                                           connectionCookie=229174615)]
        sent = vs_.uplink_specs(was, ["vmnic2"], [("41", "dvportgroup-23")])
        ck("mgmt live: the new nic carries a free port's keys",
           [q.pnicDevice for q in sent] == ["vmnic3", "vmnic2"]
           and sent[1].uplinkPortKey == "41"
           and sent[1].uplinkPortgroupKey == "dvportgroup-23")
        # a nic belongs to one switch at a time, so the release travels with the claim
        hv, _ = _host(pnics=("vmnic2",))
        rel = vs_.release_pnics(hv.configManager.networkSystem.networkInfo, ["vmnic2"])
        ck("release: the standard switch that holds the nic is edited to let it go",
           len(rel) == 1 and rel[0].name == "vSwitch0" and rel[0].changeOperation == "edit"
           and rel[0].spec.bridge is None)
        hv2, _ = _host(pnics=("vmnic2", "vmnic9"))
        rel2 = vs_.release_pnics(hv2.configManager.networkSystem.networkInfo, ["vmnic2"])
        ck("release: a switch keeping another nic keeps its bridge, minus the one moving",
           rel2[0].spec.bridge is not None
           and rel2[0].spec.bridge.nicDevice == ["vmnic9"]
           and rel2[0].spec.numPorts == 128)
        ck("release: the teaming policy stops naming the nic that left",
           rel[0].spec.policy.nicTeaming.nicOrder.activeNic == []
           and rel2[0].spec.policy.nicTeaming.nicOrder.activeNic == ["vmnic9"])
        ck("release: and the host's own spec is not edited in place",
           hv.configManager.networkSystem.networkInfo.vswitch[0]
           .spec.policy.nicTeaming.nicOrder.activeNic == ["vmnic2"])
        ck("release: nothing to say about a switch that does not hold it",
           vs_.release_pnics(hv.configManager.networkSystem.networkInfo, ["vmnic7"]) == [])

        ck("mgmt live: the nic already there keeps its port, and loses the server's cookie",
           sent[0].uplinkPortKey == "32" and sent[0].uplinkPortgroupKey == "dvportgroup-23"
           and sent[0].connectionCookie is None)
        s, d = _move(h, _dvs_(h), apply_=False)
        ck("mgmt live: and a plan says so rather than calling the host done",
           s == ADD and "add vmnic2" in d)

        h, seen = _host(src_vlan=0)
        s, d = _move(h, _dvs_(h, vlan=130))
        ck("mgmt live: a VLAN change is refused before anything is sent",
           s == BLOCKED and seen == {} and "VLAN 0" in d and "130" in d)
        h, seen = _host(src_vlan=0)
        ck("mgmt live: allow_vlan_change lets the move through",
           _move(h, _dvs_(h, vlan=130), allow=True)[0] == ADD and "cfg" in seen)

        h, seen = _host()
        ck("mgmt live: a trunk portgroup is refused, not guessed at",
           _move(h, _dvs_(h, trunk=True))[0] == BLOCKED and seen == {})

        h, seen = _host()
        ck("mgmt live: a host not yet on the switch is refused",
           _move(h, _dvs_(h, member=False))[0] == BLOCKED and seen == {})

        h, seen = _host(rollback=True)
        s, d = _move(h, _dvs_(h))
        ck("mgmt live: a silent host rollback is reported, not called success",
           s == BLOCKED and "rolled the change back" in d)

        h, _ = _host()
        _, _d = _move(h, _dvs_(h), pg="nope")
        ck("mgmt live: a missing portgroup points back at the vds step",
           "--only vds" in _d)
        # the VLAN to build it on is already in hand, so say it rather than make
        # someone go and look
        ck("mgmt live: and names the VLAN the vmkernel is on today",
           "VLAN 130" in _d and "Management Network" in _d)
        h2, _ = _host(src_vlan=0)
        ck("mgmt live: an untagged vmkernel reports VLAN 0, not a blank",
           "VLAN 0" in _move(h2, _dvs_(h2), pg="nope")[1])

    done = [Result(s).add(OK, "x") for s in STEPS]
    ck("next: a clean run of every step points at the import",
       "Import a vCenter" in next_line(done, True, "cmd"))
    one = [Result("upgrade").add(OK, "x")]
    ck("next: one clean step does not claim the build is finished",
       "Import a vCenter" not in next_line(one, True, "cmd")
       and "Not run: " in next_line(one, True, "cmd")
       and "mgmt" in next_line(one, True, "cmd"))
    ck("next: a plan with work points at --commit",
       next_line([Result("vds").add(ADD, "x")], False, "cmd").endswith("--commit"))
    # ---- ensure_vmk against a host that already has adapters
    class _Bag:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def _vnic(device, ip, pgkey=None, gw=None, stack=None):
        spec = _Bag(ip=_Bag(ipAddress=ip) if ip else None,
                    distributedVirtualPort=_Bag(portgroupKey=pgkey) if pgkey else None,
                    netStackInstanceKey=stack,
                    ipRouteSpec=_Bag(ipRouteConfig=_Bag(defaultGateway=gw)) if gw else None)
        return _Bag(device=device, spec=spec)

    def _vs_with(vnics):
        vs = VSphere.__new__(VSphere)
        host = _Bag(configManager=_Bag(networkSystem=_Bag(networkInfo=_Bag(vnic=vnics))))
        vs.vim = _Bag(HostSystem=object)
        vs.find = lambda kind, name: host
        return vs

    dvs_pg = _Bag(portgroup=[_Bag(name="pg-vmotion", key="dvportgroup-7")])

    st_, d_ = _vs_with([_vnic("vmk1", "10.0.141.73", "dvportgroup-7")]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False)
    ck("vmk: the right address on the right portgroup is ok", st_ == OK, (st_, d_))

    st_, d_ = _vs_with([_vnic("vmk1", "10.0.141.64", "dvportgroup-7")]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False)
    ck("vmk: an adapter already on that portgroup at another address blocks",
       st_ == BLOCKED, (st_, d_))
    ck("vmk: and the block names both addresses",
       "10.0.141.64" in d_ and "10.0.141.73" in d_, d_)
    ck("vmk: and says why adding a second one is wrong",
       "two vmkernels on one subnet" in d_, d_)

    st_, d_ = _vs_with([_vnic("vmk1", "10.9.9.9", "dvportgroup-other")]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False)
    ck("vmk: an adapter on a different portgroup does not block", st_ == ADD, (st_, d_))

    st_, d_ = _vs_with([]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False)
    ck("vmk: a host with no adapters plans the add", st_ == ADD, (st_, d_))

    # gateway: reported, never rewritten
    st_, d_ = _vs_with([_vnic("vmk1", "10.0.141.73", "dvportgroup-7",
                              gw="10.0.141.254")]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False)
    ck("vmk: a gateway the config did not ask for is reported, not changed",
       st_ == OK and "gateway 10.0.141.254" in d_ and "config says none" in d_, d_)

    st_, d_ = _vs_with([_vnic("vmk1", "10.0.141.73", "dvportgroup-7")]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False, gateway="10.0.141.254")
    ck("vmk: a configured gateway that is missing on the host is reported",
       st_ == OK and "gateway none" in d_, d_)

    st_, d_ = _vs_with([_vnic("vmk1", "10.0.141.73", "dvportgroup-7",
                              gw="10.0.141.254")]).ensure_vmk(
        "esx13", dvs_pg, "pg-vmotion", "10.0.141.73", "255.255.255.0", 9000,
        "vmotion", None, False, gateway="10.0.141.254")
    ck("vmk: a matching gateway is quiet", st_ == OK and "gateway" not in d_, d_)

    ck("config: network.vmotion.gateway is a name the schema knows",
       check_keys({"sites": {"s": {"network": {"vmotion": {"gateway": "1.2.3.4"}}}}},
                  KNOWN) == [])

    ck("next: a blocked run names the step",
       "clear dns" in next_line([Result("dns").add(BLOCKED, "x")], False, "cmd"))

    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    for f in failed:
        print("  FAILED: %s" % f)
    return 1 if failed else 0


def _has_pyyaml():
    import importlib.util
    return importlib.util.find_spec("yaml") is not None


if __name__ == "__main__":
    sys.exit(main())
