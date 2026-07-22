import { timingSafeEqual } from 'node:crypto';

/**
 * Authenticate the private Python↔Node bridge channel.
 *
 * An empty expected token keeps manually launched legacy bridges working.
 * Managed gateways always provide a persisted high-entropy token.
 */
export function hasValidBridgeToken(expectedToken, authorizationHeader) {
  const expected = String(expectedToken || '');
  if (!expected) return true;

  const header = String(authorizationHeader || '');
  if (!header.startsWith('Bearer ')) return false;

  const supplied = Buffer.from(header.slice('Bearer '.length), 'utf8');
  const expectedBytes = Buffer.from(expected, 'utf8');
  if (supplied.length !== expectedBytes.length) return false;

  return timingSafeEqual(supplied, expectedBytes);
}
