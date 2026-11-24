import Fastify, { FastifyReply, FastifyRequest } from "fastify";
import rateLimit from "@fastify/rate-limit";
import { verifyBearer, KeySetUnavailableError, AuthError } from "./shared/auth/verify";
import { handleRpc, parseRpc } from "./rpc";

const PUBLIC_PATHS = ["/healthz", "/.well-known/oauth-protected-resource/mcp"];
const ALLOWED_HOSTS = (process.env.MCP_ALLOWED_HOSTS || "mcp.example.test").split(",");
const RESOURCE_METADATA = "https://mcp.example.test/.well-known/oauth-protected-resource/mcp";

function challenge(reply: FastifyReply, description: string) {
  reply
    .code(401)
    .header("WWW-Authenticate",
      `Bearer error="invalid_token", error_description="${description}", resource_metadata="${RESOURCE_METADATA}"`)
    .send({ error: "invalid_token", error_description: description });
}

export function jsonRpcError(reply: FastifyReply, status: number, id: unknown, code: number, message: string) {
  return reply.code(status).send({ jsonrpc: "2.0", id, error: { code, message } });
}

export function buildApp() {
  const app = Fastify({ logger: true, trustProxy: true });

  // 1. Authentication runs first, for every route except the public ones.
  app.addHook("onRequest", async (req: FastifyRequest, reply: FastifyReply) => {
    if (PUBLIC_PATHS.includes(req.routeOptions.url ?? "")) return;
    try {
      req.user = await verifyBearer(req.headers.authorization);
    } catch (e) {
      if (e instanceof KeySetUnavailableError) {
        // The identity provider's key set could not be fetched: not the caller's fault.
        return reply.code(503).header("Retry-After", "5").send({ error: "temporarily_unavailable" });
      }
      const description = e instanceof AuthError ? e.message : "The token is invalid or expired.";
      return challenge(reply, description);
    }
  });

  // 2. Host check, after authentication.
  app.addHook("preHandler", async (req: FastifyRequest, reply: FastifyReply) => {
    const host = (req.headers.host || "").split(":")[0];
    if (!ALLOWED_HOSTS.includes(host)) {
      return reply.code(421).send({ error: "misdirected_request" });
    }
  });

  app.register(rateLimit, {
    max: 120,
    timeWindow: "1 minute",
    keyGenerator: (req: FastifyRequest) => `${req.user?.uid}:${req.user?.clientId}`,
  });

  app.get("/healthz", async () => ({ ok: true }));

  app.get("/.well-known/oauth-protected-resource/mcp", async () => ({
    resource: "https://mcp.example.test/mcp",
    authorization_servers: ["https://id.example.test/oauth2/mcp"],
    scopes_supported: ["files.read", "documents.read"],
    bearer_methods_supported: ["header"],
  }));

  // 3. The JSON-RPC handler: batch requests are refused before any tool runs.
  app.post("/mcp", async (req: FastifyRequest, reply: FastifyReply) => {
    const rpc = parseRpc(req.body);
    if (rpc.isBatch) return jsonRpcError(reply, 400, null, -32600, "Batch requests are not supported.");
    if (!rpc.ok) return jsonRpcError(reply, 400, null, -32700, "Parse error");
    return handleRpc(req.user, rpc.message, reply);
  });

  return app;
}
