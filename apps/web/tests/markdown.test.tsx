import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "@/components/legal/Markdown";

describe("Markdown (legal documents)", () => {
  it("renders headings, lists, tables, quotes and links to other documents", () => {
    const src = [
      "# Terms",
      "",
      "> **Draft for legal review.**",
      "",
      "## 1. Use",
      "See the [Acceptable Use Policy](aup.md) and `code`.",
      "",
      "1. **Every target** is yours",
      "   or you are authorized.",
      "2. Second",
      "",
      "- bullet",
      "",
      "| A | B |",
      "|---|---|",
      "| x | y |",
    ].join("\n");
    render(<Markdown source={src} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Terms");
    expect(screen.getByRole("link", { name: "Acceptable Use Policy" }).getAttribute("href")).toBe("/legal/aup");
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Every target is yours or you are authorized.",
      "Second",
      "bullet",
    ]);
    expect(screen.getByRole("table").textContent).toContain("xy");
    expect(screen.getByText("Draft for legal review.").tagName).toBe("STRONG");
  });

  it("never renders raw HTML and terminates on odd input", () => {
    const { container } = render(<Markdown source={"#nospace\n<img src=x onerror=alert(1)>\n|"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});
