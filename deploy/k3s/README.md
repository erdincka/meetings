# The lab cluster

Three k3s nodes plus a builder, on Proxmox. This replaced a `kind` cluster
running inside a Lima VM on a laptop, which constrained the project in ways that
were not obvious until they were removed:

- **gVisor ran nested four deep** — macOS Virtualization.framework, a Linux VM,
  a kind node container, then containerd. It worked, but it was the least-tested
  corner of gVisor's support matrix and nothing about it resembled a real node.
  Here each node is a VM with its own kernel and `runsc` is simply the runtime.
- **NetworkPolicy was not enforced at all.** kind's default CNI accepts policies
  and ignores them; that took a cluster rebuild with Calico to fix. k3s ships
  kube-router and enforces them out of the box — verified by the gate, not
  assumed.
- **There was no LoadBalancer**, so the Gateway was reached through a NodePort
  mapped to a host port. MetalLB now assigns a real address from the LAN.
- **Images were side-loaded** with `kind load`, because kind nodes each have
  their own image store. A registry replaces that.
- **6 cores and 11GB** became 40 cores and 141GB, which is the difference
  between one meeting at a time and a warm pool per capability profile.

## Layout

| Node | Role |
|---|---|
| k3s-cp | control plane, platform workloads |
| k3s-w1 | application |
| k3s-w2 | sandboxes — labelled `node-role=sandbox` and tainted |
| k3s-builder | image builds; not part of the cluster |

Addresses, the Proxmox host, and the inference endpoint all come from
`lab.env`, which is gitignored. Copy `lab.env.example` and edit it before
anything else — nothing here has a default that will work on your network.

The builder exists because the development laptop is arm64 and the nodes are
x86_64. Cross-building Python and Node images under emulation is slow enough to
hurt the inner loop, so builds run natively on the builder and push to the
registry, which MetalLB puts on a fixed LAN address.

## Rebuilding from nothing

    cp deploy/k3s/lab.env.example deploy/k3s/lab.env
    $EDITOR deploy/k3s/lab.env

`bootstrap.sh` then provisions the VMs from a Proxmox cloud-init template and
installs k3s, gVisor and the platform. It is idempotent, and each stage runs
separately because the expensive parts rarely need repeating when only the last
one failed.

    deploy/k3s/bootstrap.sh provision   # create/refresh the VMs
    deploy/k3s/bootstrap.sh k3s         # install k3s + gVisor
    deploy/k3s/bootstrap.sh platform    # MetalLB, cert-manager, CNPG, Gateway, Agent Sandbox
    make smoke                          # prove all three gates

Or `make cluster-up`, which does all of it and then runs the gates.

## MetalLB and your DHCP range

`METALLB_RANGE` must sit outside whatever your router hands out. An overlap
produces address conflicts that present as intermittent, unexplained outages
rather than as anything obviously network-related.
