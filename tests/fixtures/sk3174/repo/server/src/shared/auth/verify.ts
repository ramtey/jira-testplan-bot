import { createRemoteJWKSet, jwtVerify, errors } from "jose";

export class AuthError extends Error {}
export class KeySetUnavailableError extends Error {}

const ISSUER = process.env.MCP_ISSUER || "https://id.example.test/oauth2/mcp";
const AUDIENCE = process.env.MCP_AUDIENCE || "api://mcp";
const jwks = createRemoteJWKSet(new URL(`${ISSUER}/v1/keys`));

export function bearerToken(header: string | undefined): string {
  if (!header) throw new AuthError("A bearer token is required.");
  const m = /^Bearer ([A-Za-z0-9\-._~+/]+=*)$/.exec(header);
  if (!m) throw new AuthError("The token is invalid or expired.");
  return m[1];
}

export async function verifyBearer(header: string | undefined) {
  const token = bearerToken(header);
  try {
    const { payload } = await jwtVerify(token, jwks, { issuer: ISSUER, audience: AUDIENCE });
    return { uid: String(payload.uid), clientId: String(payload.cid), scopes: String(payload.scp || "").split(" ") };
  } catch (e) {
    if (e instanceof errors.JWKSTimeout || (e as { code?: string })?.code === "ERR_JWKS_FETCH") {
      throw new KeySetUnavailableError("key set unavailable");
    }
    throw new AuthError("The token is invalid or expired.");
  }
}
