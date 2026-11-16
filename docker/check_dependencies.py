"""Run pip check with the known NVIDIA SBSA wheel metadata exception."""

from importlib.metadata import distribution
import platform
import subprocess
import sys


def valid_sbsa_wheel():
    wheel = distribution("nvidia-cusparselt-cu13")
    if "Tag: py3-none-manylinux2014_sbsa" not in (wheel.read_text("WHEEL") or ""):
        return False
    for file in wheel.files or []:
        if file.name == "libcusparseLt.so.0":
            with wheel.locate_file(file).open("rb") as library:
                elf = library.read(20)
            return elf.startswith(b"\x7fELF\x02\x01") and int.from_bytes(elf[18:20], "little") == 183
    return False


result = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
expected = "nvidia-cusparselt-cu13 0.8.0 is not supported on this platform"
if result.returncode and platform.machine() == "aarch64" and result.stdout.strip() == expected:
    if valid_sbsa_wheel():
        sys.stdout.write("Verified the AArch64 NVIDIA cuSPARSELt binary with SBSA wheel metadata.\n")
        sys.exit(0)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
