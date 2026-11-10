"""Create a local glTF preview from the unmodified supplier STEP file."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading


DEFAULT_MAX_BYTES = 6 * 1024 * 1024
_OCP_LOCK = threading.Lock()
# Windows can reject two simultaneous replacements of the same destination
# even when each caller has its own temporary file.  Keep the expensive STEP
# conversion concurrent, but serialize the final publication operation.
_PUBLISH_LOCK = threading.Lock()


class StepPreviewError(RuntimeError):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = error_code


def _error(message: str, code: str) -> StepPreviewError:
    return StepPreviewError(message, error_code=code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _export_step_glb(step: Path, output: Path) -> None:
    try:
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Message import Message_ProgressRange
        from OCP.RWGltf import RWGltf_CafWriter
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
        from OCP.TColStd import TColStd_IndexedDataMapOfStringString
        from OCP.TDF import TDF_LabelSequence
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
    except ImportError as exc:
        raise _error("Local STEP preview requires cadquery-ocp-novtk.",
                     "step_preview_dependency_missing") from exc

    # OCCT STEP readers use shared settings; serialize in-process conversions.
    with _OCP_LOCK:
        document = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        if reader.ReadFile(str(step)) != IFSelect_RetDone:
            raise _error("The supplier STEP file could not be read.", "step_preview_invalid")
        reader.ChangeReader().SetSystemLengthUnit(1.0)
        if not reader.Transfer(document, Message_ProgressRange()):
            raise _error("The supplier STEP geometry could not be transferred.",
                         "step_preview_invalid")
        XCAFDoc_DocumentTool.SetLengthUnit_s(document, 0.001)
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
        labels = TDF_LabelSequence()
        shape_tool.GetFreeShapes(labels)
        if labels.IsEmpty():
            raise _error("The supplier STEP contains no shapes.", "step_preview_empty")
        for index in range(1, labels.Length() + 1):
            shape = XCAFDoc_ShapeTool.GetShape_s(labels.Value(index))
            if shape.IsNull():
                raise _error("The supplier STEP contains an empty shape.", "step_preview_empty")
            mesh = BRepMesh_IncrementalMesh(shape, 0.02, False, 0.05, False)
            if not mesh.IsDone():
                raise _error("The supplier STEP geometry could not be tessellated.",
                             "step_preview_meshing_failed")
        writer = RWGltf_CafWriter(TCollection_AsciiString(str(output)), True)
        writer.SetMergeFaces(True)
        if not writer.Perform(document, TColStd_IndexedDataMapOfStringString(),
                              Message_ProgressRange()):
            raise _error("The original STEP could not be exported as GLB.",
                         "step_preview_export_failed")


def _orient_glb(data: bytes, source_sha256: str) -> bytes:
    if len(data) < 20 or struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
        raise _error("The STEP exporter produced an invalid GLB header.",
                     "step_preview_invalid_glb")
    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise _error("The GLB contains a truncated chunk.", "step_preview_invalid_glb")
        size, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        if size % 4 or offset + size > len(data):
            raise _error("The GLB contains invalid chunk lengths.", "step_preview_invalid_glb")
        chunks.append((kind, data[offset:offset + size]))
        offset += size
    if not chunks or chunks[0][0] != 0x4E4F534A:
        raise _error("The GLB is missing its JSON scene.", "step_preview_invalid_glb")
    try:
        scene = json.loads(chunks[0][1])
        if scene["asset"]["version"] != "2.0":
            raise ValueError("unsupported glTF version")
        if any(buffer.get("uri") for buffer in scene.get("buffers", [])):
            raise ValueError("external geometry buffer")
        accessors = scene["accessors"]
        primitives = [primitive for mesh in scene["meshes"] for primitive in mesh["primitives"]]
        if not any(primitive.get("mode", 4) == 4
                   and accessors[primitive["attributes"]["POSITION"]]["count"] >= 3
                   and accessors[primitive["indices"]]["count"] >= 3
                   for primitive in primitives):
            raise ValueError("no source triangles")
        nodes = scene["nodes"]
        if not scene["scenes"] or not nodes:
            raise ValueError("no source scene")
        # OCCT 7.8 exports metres but leaves the source Z-up convention in place.
        for entry in scene["scenes"]:
            original_roots = entry.get("nodes", [])
            if not original_roots or any(not isinstance(i, int) or i < 0 or i >= len(nodes)
                                         for i in original_roots):
                raise ValueError("invalid source scene roots")
            entry["nodes"] = [len(nodes)]
            nodes.append({"name": "STEP_Z_UP_TO_GLTF_Y_UP", "children": original_roots,
                          "rotation": [-math.sqrt(0.5), 0, 0, math.sqrt(0.5)]})
        scene["asset"].setdefault("extras", {})["jlc2cadence"] = {
            "source_step_sha256": source_sha256,
            "source_geometry": "unchanged; tessellated from original STEP",
            "source_up_axis": "Z", "preview_up_axis": "Y", "preview_length_unit": "metre",
        }
        encoded = json.dumps(scene, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise _error(f"The STEP exporter produced an invalid GLB scene: {exc}",
                     "step_preview_invalid_glb") from exc
    encoded += b" " * (-len(encoded) % 4)
    chunks[0] = (0x4E4F534A, encoded)
    body = b"".join(struct.pack("<II", len(chunk), kind) + chunk for kind, chunk in chunks)
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def generate_step_glb(step: Path, root: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> Path:
    """Publish ``.<step.name>.glb`` atomically without modifying the source STEP."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise _error("The preview byte limit must be a positive integer.",
                     "step_preview_invalid_limit")
    try:
        root = Path(root).resolve(strict=True)
    except OSError as exc:
        raise _error("The preview library root does not exist.", "step_preview_missing_root") from exc
    candidate = Path(step).resolve()
    if not _inside(candidate, root):
        raise _error("The STEP file is outside the selected library root.",
                     "step_preview_outside_root")
    if not candidate.is_file():
        raise _error("The original supplier STEP file is missing.", "step_preview_missing")
    if candidate.suffix.lower() not in {".step", ".stp"}:
        raise _error("The preview source must be a STEP or STP file.", "step_preview_invalid")
    output = candidate.with_name(f".{candidate.name}.glb")
    if not _inside(output.resolve(), root):
        raise _error("The GLB preview destination is outside the selected library root.",
                     "step_preview_outside_root")
    try:
        source_sha256 = _sha256(candidate)
        with tempfile.TemporaryDirectory(prefix=".step-preview-", dir=candidate.parent) as temporary:
            scratch = Path(temporary)
            snapshot = scratch / "source.step"
            shutil.copyfile(candidate, snapshot)
            if _sha256(snapshot) != source_sha256:
                raise _error("The STEP changed while its preview was being prepared.",
                             "step_preview_source_changed")
            with snapshot.open("rb") as source:
                if b"ISO-10303-21;" not in source.read(4096).upper():
                    raise _error("The source is not an ISO 10303-21 STEP file.",
                                 "step_preview_invalid")
            temporary_glb = scratch / "preview.glb"
            _export_step_glb(snapshot, temporary_glb)
            if not temporary_glb.is_file():
                raise _error("The STEP exporter did not create a GLB.",
                             "step_preview_export_failed")
            if temporary_glb.stat().st_size > max_bytes:
                raise _error(f"The original STEP preview exceeds the {max_bytes}-byte limit.",
                             "step_preview_too_large")
            converted = _orient_glb(temporary_glb.read_bytes(), source_sha256)
            if len(converted) > max_bytes:
                raise _error(f"The original STEP preview exceeds the {max_bytes}-byte limit.",
                             "step_preview_too_large")
            with temporary_glb.open("wb") as stream:
                stream.write(converted)
                stream.flush()
                os.fsync(stream.fileno())
            if _sha256(candidate) != source_sha256:
                raise _error("The STEP changed during preview generation; preview was not published.",
                             "step_preview_source_changed")
            if not _inside(output.resolve(), root):
                raise _error("The GLB preview destination moved outside the library root.",
                             "step_preview_outside_root")
            with _PUBLISH_LOCK:
                os.replace(temporary_glb, output)
    except StepPreviewError:
        raise
    except Exception as exc:
        raise _error(f"Local STEP preview failed: {exc}", "step_preview_failed") from exc
    return output


def is_current_step_glb(step: Path, root: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Check preview provenance against current STEP bytes, regardless of timestamps."""
    try:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            return False
        root = Path(root).resolve(strict=True)
        step = Path(step).resolve(strict=True)
        output = step.with_name(f".{step.name}.glb").resolve(strict=True)
        if not _inside(step, root) or not _inside(output, root):
            return False
        if step.suffix.lower() not in {".step", ".stp"}:
            return False
        if not 20 <= output.stat().st_size <= max_bytes:
            return False
        with output.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            return False
        source_sha256 = _sha256(step)
        _orient_glb(data, source_sha256)
        json_size = struct.unpack_from("<I", data, 12)[0]
        scene = json.loads(data[20:20 + json_size])
        metadata = scene["asset"]["extras"]["jlc2cadence"]
        return (metadata["source_step_sha256"] == source_sha256
                and metadata["source_up_axis"] == "Z"
                and metadata["preview_up_axis"] == "Y"
                and metadata["preview_length_unit"] == "metre")
    except (OSError, ValueError, KeyError, TypeError, StepPreviewError):
        return False


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        # Keep native OCCT diagnostics out of the machine-readable JSON response.
        sys.stdout.flush()
        saved_stdout = os.dup(sys.stdout.fileno())
        try:
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            output = generate_step_glb(args.step, args.root, args.max_bytes)
        finally:
            sys.stdout.flush()
            os.dup2(saved_stdout, sys.stdout.fileno())
            os.close(saved_stdout)
        result = {"ok": True, "glb": str(output), "size": output.stat().st_size}
        code = 0
    except StepPreviewError as exc:
        result = {"ok": False, "error": str(exc), "error_code": exc.error_code}
        code = 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
