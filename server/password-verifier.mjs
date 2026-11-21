import { DurableObject } from 'cloudflare:workers';
import { verifyPassword } from './project-api.mjs';

/**
 * Internal password-stretching service. Only the bound Worker can call it;
 * there is no public fetch endpoint and no password or hash is stored or logged.
 * SQLite-backed Durable Objects are supported on Workers Free and have a
 * separate 30-second default CPU budget per invocation.
 */
export class ProjectPasswordVerifier extends DurableObject {
  async verify(password, encoded) {
    return await verifyPassword(password, encoded);
  }
}
