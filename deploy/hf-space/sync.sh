#!/usr/bin/env bash
# Deploy the remote MCP server to a Hugging Face Space (Docker SDK).
#
# The Space repo is separate from this one and must gather the server source
# plus the git-ignored warehouse DB, so this script assembles and pushes it.
# It reuses Dockerfile.mcp verbatim (HF builds ./Dockerfile), so there is only
# one image definition to maintain.
#
# One-time prereqs:
#   pip install huggingface_hub      # provides the `huggingface-cli` / `hf` CLI
#   huggingface-cli login            # sets Git credentials for huggingface.co
#   git lfs install
#   # create the Space once (Docker SDK): https://huggingface.co/new-space
#
# Deploy / redeploy (after `python -m src.warehouse build`):
#   HF_SPACE=<user>/scprs-warehouse-mcp bash deploy/hf-space/sync.sh
set -euo pipefail
: "${HF_SPACE:?set HF_SPACE=<user>/<space-name>, e.g. muntherhasan1/scprs-warehouse-mcp}"

root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
[ -f "$root/data/warehouse.db" ] || {
  echo "data/warehouse.db not found — build it first: python -m src.warehouse build" >&2
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
echo "Cloning Space $HF_SPACE ..."
git clone "https://huggingface.co/spaces/$HF_SPACE" "$work"
cd "$work"
git lfs install --local

mkdir -p src data
cp "$root/deploy/hf-space/README.md"      README.md
cp "$root/deploy/hf-space/.gitattributes" .gitattributes
cp "$root/Dockerfile.mcp"                 Dockerfile          # HF builds ./Dockerfile
cp "$root/requirements-mcp.txt"           requirements-mcp.txt
cp "$root/src/mcp_server.py"              src/mcp_server.py
cp "$root/src/__init__.py"                src/__init__.py
cp "$root/data/warehouse.db"              data/warehouse.db   # LFS via .gitattributes

git add -A
if git diff --cached --quiet; then
  echo "No changes to deploy."
  exit 0
fi
git commit -m "Deploy SCPRS warehouse MCP server"
git push

echo
echo "Pushed. If you haven't yet: add secret MCP_AUTH_TOKEN in the Space Settings."
echo "Endpoint: https://${HF_SPACE%%/*}-${HF_SPACE##*/}.hf.space/mcp"
