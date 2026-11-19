import json
from pathlib import Path
import unittest


class InputShapeTest(unittest.TestCase):
    def test_messages_declare_schema_version(self):
        rows = json.loads((Path(__file__).parents[1] / "data" / "messages.json").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["schema_version"] for row in rows))


if __name__ == "__main__":
    unittest.main()
