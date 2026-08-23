# kind node image with the gVisor (runsc) runtime baked in.
#
# NOTE ON ARCH: gVisor publishes its release artifacts under "aarch64" and
# "x86_64" -- NOT "arm64"/"amd64". Using the Go-style names 404s.
ARG KIND_NODE_IMAGE=kindest/node:v1.36.1
FROM ${KIND_NODE_IMAGE}

ARG GVISOR_ARCH=aarch64
ARG GVISOR_RELEASE=latest

RUN set -eux; \
    base="https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/${GVISOR_ARCH}"; \
    curl -fsSL -o /usr/local/bin/runsc                   "${base}/runsc"; \
    curl -fsSL -o /usr/local/bin/containerd-shim-runsc-v1 "${base}/containerd-shim-runsc-v1"; \
    chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1; \
    runsc --version

# runsc flags. systrap is the default platform and needs no KVM, which matters
# because we are already nested inside a macOS Virtualization.framework VM.
# ignore-cgroups avoids runsc fighting the kind node over cgroup ownership.
COPY runsc.toml /etc/containerd/runsc.toml
