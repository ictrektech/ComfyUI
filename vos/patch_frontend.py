"""Bind the pinned frontend's model download dispatcher to the VOS bridge."""
from importlib.metadata import distribution

DISPATCHER = "missingModelMetadata-DdJ7s-5P.js"
SCANNER = "missingModelScan-C_pOtMoV.js"


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
            '&&/^https:\\/\\/(huggingface\\.co|(www\\.)?modelscope\\.cn)\\//.test(e.url)'
            '&&(e.directory===\x60diffusers\x60||/\\.(safetensors|sft|gguf|ckpt|pt|pth|bin|onnx)$/i.test(e.name)))return!0;',
    }
    asset = assets / DISPATCHER
    source = asset.read_text()
    if any(source.count(anchor) != 1 for anchor in patches):
        raise SystemExit("Could not identify the pinned missing-model download dispatcher")
    for anchor, replacement in patches.items():
        source = source.replace(anchor, replacement, 1)
    asset.write_text(source)
    scanner = assets / SCANNER
    source = scanner.read_text()
    anchor = "function scanComboWidget(e,t,n,r){let i=e.valueWidget.value;if(typeof i!=\x60string\x60||!i.trim()||!isModelFileName(i))return null;"
    if source.count(anchor) != 1:
        raise SystemExit("Could not identify the pinned Diffusers widget scanner")
    source = source.replace(anchor, anchor.replace("!isModelFileName(i)",
                            "(!isModelFileName(i)&&!(window.vosComfyModelDownload&&e.nodeType===\x60DiffusersLoader\x60&&e.definitionWidgetName===\x60model_path\x60))"), 1)
    widget = "isAssetSupported:a,name:i,directory:n?.(e.nodeType)"
    if source.count(widget) != 1:
        raise SystemExit("Could not identify the pinned combo widget directory")
    scanner.write_text(source.replace(widget, widget.replace("directory:n?", "directory:e.nodeType===\x60DiffusersLoader\x60?\x60diffusers\x60:n?"), 1))


if __name__ == "__main__":
    main()
