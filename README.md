# vSphere 8 clusters, ready to import

Build a vSphere 8 cluster outside VCF to the point VCF 9.1 will import it as a workload domain: DNS, the host build check, the appliance deploy or upgrade, the cluster, NTP, the distributed switch, the vmkernels, the management migration, vSAN, the vLCM image, SSH on the appliance, and the import prerequisites. Plan by default. Nothing is written without `--commit`.

License: GPL-3.0. Built and proved out for [essential.coach](https://essential.coach).
Full write-up: [Importing ESXi 8 Into VCF 9.1 When ESX 9 Won't Run on the Hardware](https://essential.coach/importing-esxi-8-into-vcf-9-1/)

The example config calls the sites `site-a` and `site-b`, and the hosts are `example.com`. They are not a design.

## Run

```bash
python3 deploy_vc.py selftest                                   # 246 checks, no environment needed
cp deploy_vc.example.yaml env_vc8.yaml                          # then edit the sites, hosts and ranges
python3 deploy_vc.py build --config env_vc8.yaml                # plan: reads everything, writes nothing
python3 deploy_vc.py build --config env_vc8.yaml --commit
python3 deploy_vc.py build --config env_vc8.yaml --only image   # one step; repeatable
```

The steps, in order: `dns`, `esx`, `vcsa`, `upgrade`, `objects`, `hosts`, `ntp`, `vds`, `vmk`, `mgmt`, `vsan`, `image`, `ssh`, `ready`. `ntp` sits right after `hosts` because vSAN wants the clocks together before it forms a cluster and the import refuses a host whose time is unmanaged. `mgmt` moves vmk0 and the uplink carrying it in one host transaction; that uplink stays out of `network.pnics`. `image` seeds the cluster's vLCM image from a host already in it: the import refuses a baseline-managed cluster.

`--precheck` checks the appliance deploy or upgrade without touching anything. `--vendor-precheck` also runs vcsa-deploy's own precheck, which on a 9.x upgrade updates vLCM on the source appliance, so it needs `--commit`. `--set NAME=VALUE` changes one config value by its full name, then stops. `--detailed-exitcode`: 0 nothing pending, 2 pending, 1 blocked.

Every step reads the live environment and plans against it, so a run that stopped part way is finished by running the same command again. After every write the object is read back and reported as it is now, not as the API said.

Not done here: licensing, and the import itself. Standalone hosts that still need to reach the bar VCF checks are [esxi-vcf-host-prep](https://github.com/noahfarshad/esxi-vcf-host-prep).

Passwords come from the config or are asked for by name, and never reach the run log (`/tmp/deploy_vc/<timestamp>/console.txt`).

Needs `pyvmomi` from the `vcsa` step on. Run it with any Python: if the one you typed lacks pyvmomi, the tool looks for one that has it and re-runs itself under it. If no Python on the machine has it, `python3 deploy_vc.py bootstrap` makes a virtualenv the tool then finds on its own. PyYAML is optional.
