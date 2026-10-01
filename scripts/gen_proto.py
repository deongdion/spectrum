"""Regenerate the iMessage gRPC stubs from the vendored ``proto/`` tree.

The upstream ``.proto`` files ship inside ``@photon-ai/advanced-imessage`` on npm.
They import ``google/api/annotations.proto`` for HTTP transcoding hints that the
gRPC transport never uses, so those options are stripped before compiling. The
generated modules are then rewritten to import each other through the
``spectrum.providers.imessage._proto`` package instead of a top-level
``photon`` package.

Usage::

    .venv/Scripts/python scripts/gen_proto.py
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "proto"
OUT = ROOT / "spectrum" / "providers" / "imessage" / "_proto"
PKG = "spectrum.providers.imessage._proto"

_HTTP_OPTION = re.compile(r"\s*option \(google\.api\.http\) = \{.*?\};", re.DOTALL)
_HTTP_IMPORT = re.compile(r'^import "google/api/[^"]+";\n', re.MULTILINE)
_PY_IMPORT = re.compile(r"^from photon\.imessage\.v1 import", re.MULTILINE)


def _strip_http_annotations(text: str) -> str:
    text = _HTTP_IMPORT.sub("", text)
    text = _HTTP_OPTION.sub("", text)
    # `rpc Foo(...) returns (...) {}` with an empty body is valid proto3.
    return text


def main() -> int:
    protos = sorted((SRC / "photon").rglob("*.proto"))
    if not protos:
        print(f"no .proto files under {SRC}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for proto in protos:
            rel = proto.relative_to(SRC)
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_strip_http_annotations(proto.read_text(encoding="utf-8")), encoding="utf-8")

        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)

        well_known = Path(protoc.__file__).parent / "_proto"
        args = [
            "grpc_tools.protoc",
            f"-I{staging}",
            f"-I{well_known}",
            f"--python_out={OUT}",
            f"--pyi_out={OUT}",
            f"--grpc_python_out={OUT}",
            *[str(staging / p.relative_to(SRC)) for p in protos],
        ]
        if protoc.main(args) != 0:
            print("protoc failed", file=sys.stderr)
            return 1

    for path in OUT.rglob("*.py*"):
        text = path.read_text(encoding="utf-8")
        rewritten = _PY_IMPORT.sub(f"from {PKG}.photon.imessage.v1 import", text)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")

    for directory in [OUT, *[d for d in OUT.rglob("*") if d.is_dir()]]:
        (directory / "__init__.py").touch()

    print(f"generated {len(protos)} protos into {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
