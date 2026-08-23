# -*- mode: Python -*-
# Inner dev loop against the local kind cluster.
#
# Replaces the previous Tiltfile, which targeted the rancher-desktop k3s
# context and pushed to an HPE PCAI registry. The cluster is now kind with
# gVisor-backed Agent Sandbox; see deploy/kind/cluster.yaml and the Makefile.

allow_k8s_contexts('kind-meetings')

# kind loads images directly from the daemon -- no registry round trip needed.
docker_build(
    'meetings-backend',
    './backend',
    dockerfile='./backend/Dockerfile',
    live_update=[
        sync('./backend/app', '/app/app'),
        sync('./backend/scripts', '/app/scripts'),
        run('uv sync --frozen --no-dev',
            trigger=['./backend/pyproject.toml', './backend/uv.lock']),
    ],
)

docker_build(
    'meetings-frontend',
    './frontend',
    dockerfile='./frontend/Dockerfile',
    live_update=[
        sync('./frontend', '/app'),
        run('npm ci',
            trigger=['./frontend/package.json', './frontend/package-lock.json']),
    ],
)

# The persona runtime image is added in Phase 2 (sandbox/runtime).
# The exec-python and policy images are added in Phase 3.

# App chart lands in Phase 1 at deploy/charts/meetings. Until then the platform
# is brought up with `make kind-up`, which is authoritative for the cluster.
# k8s_yaml(helm('./deploy/charts/meetings', name='meetings', namespace='meetings'))
