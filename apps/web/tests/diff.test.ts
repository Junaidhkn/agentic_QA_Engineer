import { describe, expect, it } from "vitest";

import { parseDiff } from "@/components/repairs/DiffViewer";

const DIFF = `--- a/app/routes.py
+++ b/app/routes.py
@@ -10,3 +10,3 @@
 def f():
-    return 1
+    return 2
 # end
--- /dev/null
+++ b/tests/test_x.py
@@ -0,0 +1,2 @@
+def test_x():
+    assert True
`;

describe("parseDiff", () => {
  it("splits files, counts changes and numbers lines", () => {
    const files = parseDiff(DIFF);
    expect(files.map((f) => [f.path, f.status, f.additions, f.deletions])).toEqual([
      ["app/routes.py", "modified", 1, 1],
      ["tests/test_x.py", "added", 2, 0],
    ]);
    const del = files[0].lines.find((l) => l.kind === "del");
    const add = files[0].lines.find((l) => l.kind === "add");
    expect([del?.oldNo, add?.newNo]).toEqual([11, 11]);
  });
});
