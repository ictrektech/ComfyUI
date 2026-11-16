#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
usage() { echo "Usage: $0 {amd|amd-without-cuda|arm|arm-without-cuda|l4t|thor-spark} --revision <source-revision> [--verify-only]" >&2; exit 2; }
[[ $# -ge 3 && $# -le 4 && "$2" == --revision && "$3" =~ ^[a-f0-9]{7,40}$ ]] || usage
profile=$1
revision=$3
mode=${4:-}
[[ -z "$mode" || "$mode" == --verify-only ]] || usage
runtime=wheel
case "$profile" in
  amd)             arch=amd64; sheet=AMD_with_cuda; index=cu128 ;;
  amd-without-cuda) arch=amd64; sheet=AMD_with_mxn100; index=cpu ;;
  arm)             arch=arm64; sheet=ARM_with_cuda; index=cu130 ;;
  arm-without-cuda) arch=arm64; sheet=ARM_without_cuda; index=cpu ;;
  l4t)             arch=arm64; sheet=l4t; index=vendor; runtime=l4t ;;
  thor-spark)      arch=arm64; sheet=thor_spark; index=cu130 ;;
  *) usage ;;
esac
case "$(uname -m):$arch" in x86_64:amd64|aarch64:arm64) ;; *) echo "Use a native $arch host" >&2; exit 1 ;; esac
if [[ "$arch" == amd64 ]]; then
    base=swr.cn-southwest-2.myhuaweicloud.com/ictrek/python:3.12-slim-bookworm-amd
else
    base=swr.cn-southwest-2.myhuaweicloud.com/ictrek-arm/python:3.12-slim
fi
repository=swr.cn-southwest-2.myhuaweicloud.com/ictrek/comfyui
date_tag=$(TZ=Asia/Shanghai date +%Y%m%d)
tag="${profile//-/_}_${date_tag}_${revision}"
image="$repository:$tag"
build="$HOME/dev/build/ComfyUI"
mkdir -p "$build/tmp"
export TMPDIR="$build/tmp" PYTHONDONTWRITEBYTECODE=1
docker buildx build --builder default --load --provenance=false --sbom=false --platform "linux/$arch" \
    --build-arg "PYTHON_BASE=$base" --build-arg "TORCH_RUNTIME=$runtime" --build-arg "TORCH_INDEX=$index" \
    -f "$root/docker/Dockerfile.vos" -t "$image" "$root"
device=cuda
docker_args=()
if [[ "$index" == cpu ]]; then
    device=cpu
else
    docker_args+=(--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility)
fi
if [[ "$profile" == l4t ]]; then device=l4t; fi
docker run --rm "${docker_args[@]}" --entrypoint python3 "$image" -c \
    'import sys,torch,torchvision; gpu=sys.argv[1]!="cpu"; assert torch.cuda.is_available() if gpu else torch.version.cuda is None; device="cuda" if gpu else "cpu"; assert (torch.ones((2,2),device=device) @ torch.ones((2,2),device=device)).sum().item()==8; print(torch.__version__, device)' "$device"
docker run --rm "${docker_args[@]}" -e "COMFYUI_DEVICE=$device" -e VOS_USERINFO_URL=http://127.0.0.1/unused \
    --entrypoint python3 "$image" vos/smoke.py
if [[ "$mode" == --verify-only ]]; then exit 0; fi
docker push "$image"
docker manifest inspect "$image" >/dev/null
FEISHU_CONFIG_FILE="$HOME/.feishu.json" python3 "$root/ictrek.app/scripts/record_image.py" \
    --sheet "$sheet" --component comfyui --repository "$repository" --tag "$tag"
