"""Bind the pinned frontend's model download dispatcher to the VOS bridge."""
from importlib.metadata import distribution

DISPATCHER = "missingModelMetadata-DdJ7s-5P.js"


def main():
    package = distribution("comfyui-frontend-package")
    if package.version != "1.58.5":
        raise SystemExit("Review the missing-model download integration for the new frontend")
    assets = package.locate_file("comfyui_frontend_package/static/assets")
    patches = {
        'if(r.host===`browser`)return openUrlInNewTab(e.url,e.name),{status:`browser-requested`}':
            'if(r.host===`browser`){if(window.vosComfyModelDownload)window.vosComfyModelDownload(e);'
            'else openUrlInNewTab(e.url,e.name);return{status:`browser-requested`}}',
        "function isModelDownloadable(e){":
            "function isModelDownloadable(e){if(window.vosComfyModelDownload"
            '&&/^https:\\/\\/(www\\.)?modelscope\\.cn\\//.test(e.url)&&/\\.(safetensors|sft)$/.test(e.name))return!0;',
    }
    asset = assets / DISPATCHER
    source = asset.read_text()
    if any(source.count(anchor) != 1 for anchor in patches):
        raise SystemExit("Could not identify the pinned missing-model download dispatcher")
    for anchor, replacement in patches.items():
        source = source.replace(anchor, replacement, 1)
    asset.write_text(source)


if __name__ == "__main__":
    main()
