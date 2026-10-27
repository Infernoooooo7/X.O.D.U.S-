// What an error code means for the user: a title and, where there is one, what to do. Shared by live replies and
// saved turns, so a failure reads the same whether it just happened or is shown again from the history.

const TITLES: Record<string, string> = {
  provider_unavailable: "Ollama is not available",
  provider_timeout: "Ollama took too long",
  model_not_available: "The model is not installed",
  queue_full: "XODUS is busy",
  input_too_large: "The message is too long for the model's context",
  stream_incomplete: "The reply stopped early",
  backend_unreachable: "The XODUS backend is not answering",
  internal_error: "Unexpected backend error",
};

const HINTS: Record<string, string> = {
  provider_unavailable: "The backend is still running. Start Ollama with scripts\\start-ollama.ps1, then send again.",
  queue_full: "Another reply is being generated and the queue is full. Wait a moment and send again.",
  model_not_available: "Install it with ollama pull, or switch to an installed profile in Models.",
  backend_unreachable: "Start the backend and try again.",
  input_too_large: "Start a new conversation or shorten the message: earlier turns count towards the context.",
};

export function errorTitle(code: string | undefined): string | undefined {
  if (!code || code === "cancelled") return undefined;
  return TITLES[code] ?? "The reply failed";
}

export function errorHint(code: string | undefined): string | undefined {
  return code ? HINTS[code] : undefined;
}
