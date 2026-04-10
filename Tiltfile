# -*- mode: Python -*-
# Tiltfile for Meeting Simulator

# Load .env file
load('ext://dotenv', 'dotenv')
dotenv()

# Configuration
# Prerequisite: rancher-desktop context
allow_k8s_contexts(os.environ['KUBE_CONTEXT'])
default_registry('registry.' + os.environ['DOMAIN'])

# Global namespace - standardise on 'dev'
namespace = 'dev'

# 1. Backend Build & Live Update
docker_build(
    'meetings-backend',
    './backend',
    dockerfile='./backend/Dockerfile',
    # Enable live updates to avoid full rebuilds
    live_update=[
        sync('./backend/app', '/app/app'),
        sync('./backend/scripts', '/app/scripts'),
        # Re-sync dependencies if locked files change
        run('uv sync --frozen --no-dev', trigger=['./backend/pyproject.toml', './backend/uv.lock']),
    ]
)

# 2. Frontend Build
# Note: Next.js standalone build is complex for live update in Tilt.
# For now, we perform full builds or assume simple sync.
docker_build(
    'meetings-frontend',
    './frontend',
    dockerfile='./frontend/Dockerfile',
    # Enable live updates for frontend hot-reload
    live_update=[
        sync('./frontend', '/app'),
        # Re-install dependencies if lock files change
        run('npm ci', trigger=['./frontend/package.json', './frontend/package-lock.json']),
    ]
)

# 3. Helm Chart Deployment
# We deploy the entire chart into the 'dev' namespace.
# We set image paths to the images built by Tilt.
helm_yaml = helm(
    './helm',
    name='meetings',
    namespace=namespace,
    set=[
        'ezua.virtualService.endpoint=meetings.' + os.environ['DOMAIN'],
        'global.env=development',
        'backend.image=registry.' + os.environ['DOMAIN'] + '/meetings-backend:latest',
        'frontend.image=registry.' + os.environ['DOMAIN'] + '/meetings-frontend:latest',
    ]
)

# 4. Deploy to Kubernetes
k8s_yaml(helm_yaml)
