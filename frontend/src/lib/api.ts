// Client for the Maestro /api/v1 contract: envelope unwrapping, CSRF header for cookie
// sessions and typed errors. Every call is typed by its OpenAPI path (`paths` in the
// generated schema.d.ts): path parameters, query, body, required headers and the returned
// `data` all come from the contract, so a caller cannot pick the wrong response type.
// The CSRF token lives in memory only.
import type { paths } from "./schema";
import type { ApiErrorBody, Meta } from "./types";

export const API = "/api/v1";

let csrfToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody;
  requestId?: string;
  /** True when the outcome is unknown (network failure, 5xx) and a retry may be needed. */
  indeterminate: boolean;

  constructor(status: number, body: ApiErrorBody, requestId?: string, indeterminate = false) {
    super(body.message);
    this.status = status;
    this.body = body;
    this.requestId = requestId;
    this.indeterminate = indeterminate;
  }

  get code() {
    return this.body.code;
  }

  /** Message for a form field: path "/name" or "/task_selection/task_ids" ending with the key. */
  field(name: string): string | undefined {
    const found = this.body.field_errors.find(
      (f) => f.path === `/${name}` || f.path.endsWith(`/${name}`) || f.path === name,
    );
    if (!found) return undefined;
    const limit = found.limit != null ? `（限制 ${found.limit}）` : "";
    return `${found.message ?? found.code}${limit}`;
  }
}

export interface Page<T> {
  data: T;
  meta: Meta;
}

// --- types derived from the contract ---------------------------------------------------

type Prefix = "/api/v1";
type Method = "get" | "post" | "put" | "delete" | "patch";
/** API paths as callers write them: the OpenAPI key without the /api/v1 prefix. */
type ApiPath = {
  [K in keyof paths]: K extends `${Prefix}${infer Rest}` ? Rest : never;
}[keyof paths];
type Operation<P extends ApiPath, M extends Method> = paths[`${Prefix}${P}`][M];
/** The paths that have an operation for this method. */
export type PathsFor<M extends Method> = {
  [P in ApiPath]: paths[`${Prefix}${P}`] extends Record<M, { responses: unknown }> ? P : never;
}[ApiPath];

type SuccessCode = 200 | 201 | 202 | 204;
type Successes<O> = O extends { responses: infer R }
  ? { [C in keyof R]: C extends SuccessCode ? R[C] : never }[keyof R]
  : never;
type JsonContent<R> = R extends { content: { "application/json": infer J } } ? J : never;
type EnvelopeData<E> = E extends { data: infer D } ? D : never;
/** What `data` holds on success; `undefined` for 204 answers. */
export type DataOf<O> = [JsonContent<Successes<O>>] extends [never]
  ? undefined
  : EnvelopeData<JsonContent<Successes<O>>>;

type Params<O> = O extends { parameters: infer P } ? P : never;
type Part<O, K extends string> = K extends keyof Params<O> ? NonNullable<Params<O>[K]> : never;
type BodyOf<O> = O extends { requestBody?: infer R } ? NonNullable<R> : never;
type JsonBody<O> = [BodyOf<O>] extends [never]
  ? { body?: never }
  : BodyOf<O> extends { content: { "application/json": infer B } }
    ? O extends { requestBody: unknown }
      ? { body: B }
      : { body?: B }
    : { body?: never };

/** Options of one call: only what the operation declares, required when it is required. */
export type Options<O> = ([Part<O, "path">] extends [never]
  ? { path?: never }
  : { path: Part<O, "path"> }) &
  ([Part<O, "query">] extends [never] ? { query?: never } : { query?: Part<O, "query"> }) &
  ([Part<O, "header">] extends [never]
    ? { headers?: Record<string, string> }
    : { headers: Part<O, "header"> & Record<string, string> }) &
  JsonBody<O>;

/** The options argument may be left out when nothing in it is required. */
type Args<O> = object extends Options<O> ? [options?: Options<O>] : [options: Options<O>];

// --- transport -----------------------------------------------------------------------------

interface Request {
  path?: Record<string, string | number>;
  query?: Record<string, unknown>;
  headers?: Record<string, string>;
  body?: unknown;
  /** Sent as is (a file upload) instead of JSON. */
  raw?: Blob;
}

function url(template: string, options: Request): string {
  const path = template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = options.path?.[name];
    if (value === undefined) throw new Error(`missing path parameter ${name} for ${template}`);
    return encodeURIComponent(String(value));
  });
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return `${API}${path}${text ? `?${text}` : ""}`;
}

async function send<T>(method: string, template: string, options: Request = {}): Promise<Page<T>> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.raw !== undefined)
    headers["Content-Type"] ??= options.raw.type || "application/octet-stream";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let response: Response;
  try {
    response = await fetch(url(template, options), {
      method,
      headers,
      credentials: "same-origin",
      body: options.raw ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    });
  } catch (error) {
    throw new ApiError(
      0,
      {
        code: "NETWORK_ERROR",
        message: `网络请求失败：${error instanceof Error ? error.message : String(error)}`,
        field_errors: [],
        retryable: true,
        suggested_actions: [],
      },
      undefined,
      true,
    );
  }
  const requestId = response.headers.get("X-Request-Id") ?? undefined;
  if (response.status === 204) {
    return { data: undefined as T, meta: { request_id: requestId ?? "" } };
  }
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const body =
      payload && typeof payload === "object" && "error" in payload
        ? (payload as { error: ApiErrorBody }).error
        : {
            code: `HTTP_${response.status}`,
            message: response.statusText || "请求失败",
            field_errors: [],
            retryable: response.status >= 500,
            suggested_actions: [],
          };
    body.field_errors ??= [];
    if (response.status === 401 && onUnauthorized && template !== "/session") onUnauthorized();
    throw new ApiError(response.status, body, requestId, response.status >= 500);
  }
  return payload as Page<T>;
}

function call<M extends Method>(method: M) {
  return <P extends PathsFor<M>>(path: P, ...[options]: Args<Operation<P, M>>) =>
    send<DataOf<Operation<P, M>>>(method.toUpperCase(), path, (options ?? {}) as Request);
}

export const api = {
  get: call("get"),
  post: call("post"),
  put: call("put"),
  patch: call("patch"),
  del: call("delete"),
  /** A binary request body (an attachment file's content), typed by the same path. */
  upload: <P extends PathsFor<"put">>(path: P, params: Record<string, string>, file: Blob) =>
    send<DataOf<Operation<P, "put">>>("PUT", path, {
      path: params,
      raw: file,
      headers: { "Content-Type": "application/octet-stream" },
    }),
};

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.body.message}（${error.code}）`;
  return error instanceof Error ? error.message : String(error);
}
