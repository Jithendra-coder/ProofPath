export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const csrf =
    typeof document === "undefined"
      ? ""
      : document.cookie
          .split("; ")
          .find((value) => value.startsWith("__Host-proofpath_csrf="))
          ?.split("=")
          .slice(1)
          .join("=") || "";
  let response: Response;
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      signal,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        ...(body !== undefined && !isForm ? { "Content-Type": "application/json" } : {}),
        ...(method !== "GET"
          ? { "X-CSRF-Token": decodeURIComponent(csrf) }
          : {}),
      },
      ...(body !== undefined ? { body: isForm ? body : JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError("We couldn’t reach Proofpath. Please try again.", 0);
  }
  if (response.status === 204) return undefined as T;
  const result = await response.json().catch(() => null);
  if (!response.ok)
    throw new ApiError(
      typeof result?.detail === "string"
        ? result.detail
        : response.status === 401
          ? "Sign in to open your workspace."
          : "We couldn’t complete that request. Please try again.",
      response.status,
    );
  if (result === null)
    throw new ApiError(
      "The service returned an unexpected response. Please try again.",
      response.status,
    );
  return result as T;
}

export function messageOf(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}
