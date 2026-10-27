import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorNote, NumberedCard, Status, Ticket } from "../xodus/parts";
import { SchemaForm } from "../xodus/schema-form";
import { CitedText } from "../xodus/citations";

afterEach(cleanup);

describe("design parts", () => {
  it("numbered card has the anatomy from the brief, with a real heading", () => {
    render(<NumberedCard number="03" eyebrow="text.stats v1.0.0" title="Text statistics" subtitle="Counts words." tags={<span>plugin</span>}
      action={<button>Try it</button>}>Body</NumberedCard>);
    expect(screen.getByRole("heading", { level: 2, name: "Text statistics" })).toBeTruthy();
    expect(screen.getByText("03").getAttribute("aria-hidden")).toBe("true"); // decorative number, not read twice
    expect(screen.getByRole("button", { name: "Try it" })).toBeTruthy();
  });
  it("ticket drops empty fields", () => {
    const { container } = render(<Ticket fields={[["model", "qwen3"], ["ttft", ""], ["tokens", 0]]} />);
    expect(container.textContent).toBe("model qwen3tokens 0");
  });
  it("errors are announced and carry the code", () => {
    render(<ErrorNote error={{ code: "provider_unavailable", message: "Ollama is not reachable." }} title="Ollama is not available" />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Ollama is not reachable.");
    expect(alert.textContent).toContain("code provider_unavailable");
  });
  it("status shows its label as text, not only colour", () => {
    render(<Status tone="wait">Needs your approval</Status>);
    expect(screen.getByText("Needs your approval")).toBeTruthy();
  });
});

describe("schema form", () => {
  const schema = { type: "object", required: ["text"], properties: {
    text: { type: "string", maxLength: 1000, description: "The text to measure." }, top: { type: "integer", minimum: 0, maximum: 20 } } };

  it("labels every field, shows errors next to them and submits typed arguments", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={schema} idPrefix="t" submitLabel="Run text.stats" onSubmit={onSubmit} />);
    const text = screen.getByLabelText("Text");
    const top = screen.getByLabelText("Top (optional)");
    fireEvent.click(screen.getByRole("button", { name: "Run text.stats" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(text.getAttribute("aria-invalid")).toBe("true");
    expect(text.getAttribute("aria-describedby")).toContain("t-text-error");
    fireEvent.change(text, { target: { value: "one two" } });
    fireEvent.change(top, { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Run text.stats" }));
    expect(onSubmit).toHaveBeenCalledWith({ text: "one two", top: 3 });
  });
});

describe("cited text", () => {
  it("turns each cited id into a button that names the passage", () => {
    render(<CitedText text="Use json.loads [chunk:100, chunk:7]." validIds={[100]} />);
    expect(screen.getByRole("button", { name: "Open source passage 100" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open source passage 7 (not validated)" })).toBeTruthy();
    expect(document.body.textContent).toContain("Use json.loads");
  });
});
