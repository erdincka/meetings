# -*- mode: Python -*-
# Inner development loop against your cluster.
#
# `make deploy` is the deployment path and stays authoritative. Tilt exists only
# to shorten the edit-to-running-code cycle on the two images that change most:
# it syncs source into the running containers instead of rebuilding, tagging,
# pushing and rolling.
#
# The sandbox images are deliberately absent. A persona sandbox is claimed from
# a warm pool and torn down at the end of a meeting, so live-updating one gets
# you a pod that is about to be replaced; rebuild those with `make images`.

load('ext://helm_resource', 'helm_resource')

# Explicit rather than a wildcard: Tilt rebuilds and redeploys, and doing that
# against a context you did not mean to name is an expensive mistake. Set
# TILT_CONTEXT to your own.
allow_k8s_contexts(os.getenv('TILT_CONTEXT', 'default'))

registry = str(local(
    "sed -n 's/^IMAGE_REGISTRY=//p' deploy/cluster/cluster.env",
    quiet=True,
)).strip()

if not registry:
    fail('deploy/cluster/cluster.env is missing or has no IMAGE_REGISTRY. ' +
         'Copy deploy/cluster/cluster.env.example and edit it.')

docker_build(
    registry + '/meetings-backend',
    './backend',
    dockerfile='./backend/Dockerfile',
    target='runtime',
    live_update=[
        sync('./backend/app', '/app/app'),
        sync('./backend/scripts', '/app/scripts'),
        run('uv sync --frozen --no-dev',
            trigger=['./backend/pyproject.toml', './backend/uv.lock']),
    ],
)

docker_build(
    registry + '/meetings-frontend',
    './frontend',
    dockerfile='./frontend/Dockerfile',
    target='runtime',
    live_update=[
        sync('./frontend/src', '/app/src'),
        run('npm ci',
            trigger=['./frontend/package.json', './frontend/package-lock.json']),
    ],
)

k8s_yaml(helm(
    './deploy/charts/meetings',
    name='meetings',
    namespace='meetings',
    values=['./deploy/charts/meetings/values-cluster.yaml'],
))

k8s_resource('meetings-backend', port_forwards=['8000:8000'])
k8s_resource('meetings-frontend', port_forwards=['3000:80'])
