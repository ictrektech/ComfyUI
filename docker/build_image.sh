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
wheel_args=()
wheel_dir="$build/wheels/$index"
wheel="torch-2.10.0+$index-cp312-cp312-manylinux_2_28_$(uname -m).whl"
if [[ "$runtime" == wheel && -f "$wheel_dir/$wheel" ]]; then
    # Hashes from the official PyTorch index for the exact platform wheel.
    case "$index:$arch" in
      cpu:amd64)   sha=ee40b8a4b4b2cf0670c6fd4f35a7ef23871af956fecb238fbf5da15a72650b1d ;;
      cpu:arm64)   sha=179451716487f8cb09b56459667fa1f5c4c0946c1e75fbeae77cfc40a5768d87 ;;
      cu128:amd64) sha=628e89bd5110ced7debee2a57c69959725b7fbc64eab81a39dd70e46c7e28ba5 ;;
      cu130:arm64) sha=4fc8f67637f4c92b989a07d80ffe755e79a3510ca02ebf23ce66396fb277c88d ;;
      *) exit 1 ;;
    esac
    printf '%s  %s\n' "$sha" "$wheel_dir/$wheel" | sha256sum --check --status
    runtime=cached-wheel
    wheel_args=(--build-context "torch_wheels=$wheel_dir" --build-arg "TORCH_WHEEL=$wheel")
    machine=$(uname -m)
    triton="triton-3.6.0-cp312-cp312-manylinux_2_27_$machine.manylinux_2_28_$machine.whl"
    if [[ "$index" == cu* && -f "$wheel_dir/$triton" ]]; then
        case "$arch" in
          amd64) sha=6f5928e6d44c34a97bbe164cceddc0ef2007121c89ebcfba5415cf452de7ee9f ;;
          arm64) sha=6e42d084864d12b8784736fe51a0cd05f6cc56775b25c8102c9d80c421f4e298 ;;
        esac
        printf '%s  %s\n' "$sha" "$wheel_dir/$triton" | sha256sum --check --status
        runtime=cached-cuda-wheel
        wheel_args+=(--build-arg "TRITON_WHEEL=$triton")
        vision="torchvision-0.25.0+$index-cp312-cp312-manylinux_2_28_$machine.whl"
        audio="torchaudio-2.10.0+$index-cp312-cp312-manylinux_2_28_$machine.whl"
        if [[ -f "$wheel_dir/$vision" && -f "$wheel_dir/$audio" ]]; then
            case "$arch" in
              amd64)
                vision_sha=1255a0ca2bf987acf9f103b96c5c4cfe3415fc4a1eef17fa08af527a04a4f573
                audio_sha=d26b91a173cee6db9abff68b48d64236950ffc5628d06448ecdd7ac56841e10a ;;
              arm64)
                vision_sha=3646e6c8fa5066da392d0ff13002cc683301386fe1933f8f1432fc5292e5d288
                audio_sha=f04907b7a710cd612adb63db3a2dac6265e4679686fb2b1c9e1b70562b1bda29 ;;
            esac
            printf '%s  %s\n' "$vision_sha" "$wheel_dir/$vision" "$audio_sha" "$wheel_dir/$audio" | sha256sum --check --status
            runtime=cached-cuda-complete
            wheel_args+=(--build-arg "VISION_WHEEL=$vision" --build-arg "AUDIO_WHEEL=$audio")
        fi
    fi
fi
docker buildx build --builder default --load --provenance=false --sbom=false --platform "linux/$arch" \
    --build-arg "PYTHON_BASE=$base" --build-arg "TORCH_RUNTIME=$runtime" --build-arg "TORCH_INDEX=$index" \
    "${wheel_args[@]}" -f "$root/docker/Dockerfile.vos" -t "$image" "$root"
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
