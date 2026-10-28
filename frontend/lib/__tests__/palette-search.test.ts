import { describe, expect, it } from "vitest";
import { score } from "../palette-search";

const view = { label: "Knowledge", hint: "Collections, files, ingestion", keywords: "open view page", group: "Go to" };
const ask = { label: "Ask your documents", hint: "Answers only from ingested documents", keywords: "knowledge rag search", group: "Workspace" };
const pause = { label: "Pause background ingestion", keywords: "knowledge worker", group: "System" };

describe("palette ranking", () => {
  it("ranks a label match above a keyword-only match", () => {
    expect(score(view, "knowledge")).toBe(0);
    expect(score(ask, "knowledge")).toBe(3);
  });
  it("ranks starts-with, then contains the phrase, then contains every word", () => {
    expect(score(pause, "pause")).toBe(0);
    expect(score(pause, "background ingestion")).toBe(1);
    expect(score(pause, "pause ingestion")).toBe(2);
  });
  it("needs every word somewhere, ignoring case and extra spaces", () => {
    expect(score(pause, "  PAUSE   worker ")).toBe(3);
    expect(score(pause, "pause theme")).toBeNull();
  });
  it("matches everything for an empty query", () => {
    expect(score(view, "")).toBe(0);
    expect(score(ask, "   ")).toBe(0);
  });
});
