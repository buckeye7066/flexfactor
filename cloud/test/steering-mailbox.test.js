import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

const moduleUrl = new URL('../lib/steering-mailbox.js', import.meta.url);
async function implementation() {
  assert.ok(existsSync(moduleUrl), 'Encrypted steering transport is required');
  return import(moduleUrl.href);
}
const request = 'e1f564b8-b65f-46c9-9464-46e4ce4b0607';
const repository = 'owner/disposable';

test('job steering never asks its job token to read repository variables', () => {
  const workflow = readFileSync(new URL('../../.github/workflows/mobile-run.yml', import.meta.url), 'utf8');
  assert.doesNotMatch(workflow, /\/actions\/variables\//);
  assert.match(workflow, /mobile_steering_launch\.mjs/);
});

test('steering is sealed for one request and tampering is rejected', async () => {
  const m = await implementation();
  const pair = await m.generateSteeringKeyPair();
  const secret = 'private direction: preserve all user data';
  const box = await m.sealSteering(pair.public_key, request, repository, secret);
  assert.ok(!JSON.stringify(box).includes(secret));
  const opened = await m.openSteering(pair.private_key, box, request, repository);
  assert.equal(opened.comment, secret);
  await assert.rejects(m.openSteering(pair.private_key, box, request, 'owner/other'));
  await assert.rejects(m.openSteering(pair.private_key, {...box, ciphertext: box.ciphertext.slice(0, -4) + 'AAAA'}, request, repository));
});
