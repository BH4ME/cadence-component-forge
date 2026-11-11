from copy import deepcopy
import csv
from pathlib import Path
import tempfile
import unittest

from jlc2cadence.capture_bridge import namespace_part
from jlc2cadence.generators.allegro_skill import write_padstack_specs
from jlc2cadence.ir.schema import Footprint, LibraryIR, Pad


class PadstackNamespaceTests(unittest.TestCase):
    def test_identical_geometry_in_different_parts_cannot_overwrite_native_pads(self):
        original = Pad(number="1", x=1.25, y=-2.5, kind="thru_hole", shape="circle",
                       size=(1.799996, 1.799996), hole_diameter=.999998, layers=["all"],
                       properties={"Conversion_Policy": "source_exact", "plated": "true"})
        rows = []
        with tempfile.TemporaryDirectory() as directory:
            for part_id in ("C2316", "C5208", "C698360"):
                library = LibraryIR(footprints=[Footprint(name="connector", pads=[deepcopy(original)])])
                namespace_part(library, part_id)
                pad = library.footprints[0].pads[0]
                snapshot = pad.to_dict()
                snapshot["padstack"] = ""
                self.assertEqual(snapshot, original.to_dict(), "Namespacing must not change source geometry")
                spec = Path(directory) / (part_id + ".csv")
                write_padstack_specs(library, spec)
                with spec.open(encoding="utf-8", newline="") as stream:
                    rows.append(next(csv.DictReader(stream)))
        self.assertEqual(len({row["padstack"] for row in rows}), 3)
        for row in rows:
            self.assertLessEqual(len(row["padstack"]), 31)
        self.assertEqual([{k: v for k, v in row.items() if k != "padstack"} for row in rows],
                         [{k: v for k, v in rows[0].items() if k != "padstack"}] * 3)


if __name__ == "__main__":
    unittest.main()
