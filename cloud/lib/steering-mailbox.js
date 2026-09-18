import sodium from 'libsodium-wrappers';
import { randomUUID } from 'node:crypto';

export const MAILBOX_SCHEMA = 'flexfactor-steering-v1';
export const MAX_ENVELOPE_BYTES = 32768;
export const MAX_MAILBOX_ASSETS = 100;
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const REPO = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/;
function invalid() { throw new Error('Invalid encrypted steering identity or message'); }
function keyBytes(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9+/]{43}=$/.test(value)) invalid();
  const bytes = Buffer.from(value, 'base64');
  if (bytes.length !== 32 || bytes.toString('base64') !== value) invalid();
  return bytes;
}
export function steeringSecretName(requestId) {
  if (!UUID.test(requestId)) invalid();
  return `FLEXFACTOR_${requestId.replaceAll('-', '').toUpperCase()}_STEERING_KEY`;
}
export function mailboxTag(requestId) {
  if (!UUID.test(requestId)) invalid();
  return `flexfactor-steering-${requestId.toLowerCase()}`;
}
export async function generateSteeringKeyPair() {
  await sodium.ready;
  const pair = sodium.crypto_box_keypair();
  return { public_key: Buffer.from(pair.publicKey).toString('base64'),
    private_key: Buffer.from(pair.privateKey).toString('base64') };
}
export async function sealPrivateKeyForGitHub(privateKey, repositoryKey) {
  await sodium.ready;
  keyBytes(privateKey);
  const publicKey = keyBytes(repositoryKey.key);
  if (!/^[A-Za-z0-9_-]{1,200}$/.test(repositoryKey.key_id || '')) invalid();
  return { key_id: repositoryKey.key_id, encrypted_value: Buffer.from(
    sodium.crypto_box_seal(Buffer.from(privateKey), publicKey)).toString('base64') };
}
export function mailboxMetadata(requestId, repository, publicKey) {
  if (!UUID.test(requestId) || !REPO.test(repository)) invalid();
  keyBytes(publicKey);
  return { schema: MAILBOX_SCHEMA, request_id: requestId.toLowerCase(),
    repository: repository.toLowerCase(), public_key: publicKey };
}
export function assertMailbox(release, identity) {
  if (!release || release.id !== identity.release_id || release.draft !== true
      || release.prerelease !== true || release.tag_name !== mailboxTag(identity.request_id)
      || release.author?.id !== identity.author_id
      || typeof release.body !== 'string' || release.body.length > 2048) invalid();
  let metadata;
  try { metadata = JSON.parse(release.body); } catch { invalid(); }
  const expected = mailboxMetadata(identity.request_id, identity.repository, identity.public_key);
  if (Object.keys(metadata).length !== Object.keys(expected).length
      || Object.entries(expected).some(([key, value]) => metadata[key] !== value)) invalid();
  return metadata;
}
function checkComment(comment) {
  if (typeof comment !== 'string' || !comment.trim() || comment.length > 4000
      || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(comment)) invalid();
}
export async function sealSteering(publicKey, requestId, repository, comment) {
  await sodium.ready;
  mailboxMetadata(requestId, repository, publicKey);
  checkComment(comment);
  const message = { schema: MAILBOX_SCHEMA, id: randomUUID(),
    request_id: requestId.toLowerCase(), repository: repository.toLowerCase(),
    comment, created_at: new Date().toISOString() };
  const ciphertext = Buffer.from(sodium.crypto_box_seal(
    Buffer.from(JSON.stringify(message)), keyBytes(publicKey))).toString('base64');
  return { schema: MAILBOX_SCHEMA, ciphertext };
}
export async function openSteering(privateKey, envelope, requestId, repository) {
  await sodium.ready;
  if (!UUID.test(requestId) || !REPO.test(repository) || !envelope
      || envelope.schema !== MAILBOX_SCHEMA || typeof envelope.ciphertext !== 'string'
      || envelope.ciphertext.length > MAX_ENVELOPE_BYTES
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(envelope.ciphertext)) invalid();
  const encoded = Buffer.from(envelope.ciphertext, 'base64');
  if (encoded.toString('base64') !== envelope.ciphertext) invalid();
  const secret = keyBytes(privateKey);
  let message;
  try { message = JSON.parse(sodium.to_string(sodium.crypto_box_seal_open(
    encoded, sodium.crypto_scalarmult_base(secret), secret))); } catch { invalid(); }
  if (message?.schema !== MAILBOX_SCHEMA || !UUID.test(message.id || '')
      || message.request_id !== requestId.toLowerCase()
      || message.repository !== repository.toLowerCase()
      || !Number.isFinite(Date.parse(message.created_at))) invalid();
  checkComment(message.comment);
  return message;
}
export function validateMailboxClaim(value, requestId, repository) {
  if (!value || value.schema !== 1 || !['preparing', 'ready', 'dispatching'].includes(value.phase)
      || !Number.isSafeInteger(value.release_id) || value.release_id < 0
      || !Number.isSafeInteger(value.author_id) || value.author_id <= 0
      || (value.phase !== 'preparing' && value.release_id === 0)) invalid();
  mailboxMetadata(requestId, repository, value.public_key);
  return { schema: 1, phase: value.phase, release_id: value.release_id,
    author_id: value.author_id, public_key: value.public_key };
}
export function mailboxIdentity(steering, request) {
  return { ...validateMailboxClaim(steering, request.request_id, request.repository),
    request_id: request.request_id, repository: request.repository };
}
export function assertMailboxAsset(asset, identity) {
  if (!asset || !Number.isSafeInteger(asset.id) || asset.id <= 0
      || !/^steering-[0-9a-f-]{36}\.json$/.test(asset.name || '')
      || !UUID.test(asset.name.slice(9, -5))
      || asset.uploader?.id !== identity.author_id
      || !Number.isSafeInteger(asset.size) || asset.size > MAX_ENVELOPE_BYTES || asset.size < 1) invalid();
}
export function draftMailboxPayload(request, identity) {
  return { tag_name: mailboxTag(request.request_id), target_commitish: request.ref,
    name: `FlexFactor private steering ${request.request_id}`,
    body: JSON.stringify(mailboxMetadata(request.request_id, request.repository, identity.public_key)),
    draft: true, prerelease: true, make_latest: 'false' };
}
export async function steeringPublicKey(privateKey) {
  await sodium.ready;
  return Buffer.from(sodium.crypto_scalarmult_base(keyBytes(privateKey))).toString('base64');
}
