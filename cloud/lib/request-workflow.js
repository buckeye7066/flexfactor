import {ServiceError} from './service-error.js';
import {mobileWorkflow} from './workflow.js';
import {WORKFLOW_PATH} from './config.js';

const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const SHA = /^[a-f0-9]{40}$/;
const REPO = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/;
function failure(message) { return new ServiceError(409, 'request_workflow_unavailable', message); }
function body(result) {
  try { return JSON.parse(result.body.toString('utf8')); }
  catch { throw failure('GitHub returned invalid request workflow metadata'); }
}
function successful(result) { return result.status >= 200 && result.status < 300; }
function assertRepository(repository) {
  if (!REPO.test(repository || '')) throw failure('Invalid request workflow repository');
}
export function requestWorkflowRef(requestId) {
  if (typeof requestId !== 'string' || !UUID.test(requestId)) throw failure('Invalid request workflow identity');
  return 'flexfactor-run-' + requestId.toLowerCase();
}
export function normalizeWorkflowClaim(value, requestId) {
  if (!value || value.ref !== requestWorkflowRef(requestId) || !SHA.test(value.sha || '')) {
    throw failure('Request workflow identity is invalid or changed');
  }
  return {ref: value.ref, sha: value.sha};
}

export async function assertRequestWorkflowAbsent(api, request) {
  assertRepository(request.repository);
  const ref = requestWorkflowRef(request.request_id);
  const existing = await api('GET', '/repos/' + request.repository + '/git/ref/tags/' + ref);
  if (existing.status !== 404) {
    throw failure(existing.status === 200 ? 'Request workflow tag already exists; existing work preserved' : 'Could not inspect reserved request workflow tag');
  }
}

/** Create a caller-only commit: no target source or credential value is copied.
 * The manifest is persisted before the reference write, so interrupted writes
 * retain enough identity for safe cleanup instead of guessing at ownership.
 */
export async function createRequestWorkflow(api, request, persist) {
  assertRepository(request.repository);
  const ref = requestWorkflowRef(request.request_id);
  const root = '/repos/' + request.repository;
  const referencePath = root + '/git/ref/tags/' + ref;
  await assertRequestWorkflowAbsent(api, request);
  const treeReply = await api('POST', root + '/git/trees', {tree: [{
    path: WORKFLOW_PATH, mode: '100644', type: 'blob', content: mobileWorkflow(request.request_id),
  }]});
  if (!successful(treeReply)) throw failure('Could not create request workflow tree');
  const tree = body(treeReply);
  if (!SHA.test(tree.sha || '')) throw failure('Invalid request workflow tree identity');
  const commitReply = await api('POST', root + '/git/commits', {
    message: 'Bind FlexFactor request ' + request.request_id,
    tree: tree.sha, parents: [],
  });
  if (!successful(commitReply)) throw failure('Could not create request workflow commit');
  const commit = body(commitReply);
  if (!SHA.test(commit.sha || '') || commit.tree?.sha !== tree.sha || !Array.isArray(commit.parents) || commit.parents.length) {
    throw failure('Invalid request workflow commit identity');
  }
  const manifest = {ref, sha: commit.sha};
  await persist(manifest);
  const created = await api('POST', root + '/git/refs', {ref: 'refs/tags/' + ref, sha: commit.sha});
  if (!successful(created)) {
    if (![409, 422].includes(created.status)) throw failure('Could not publish reserved request workflow tag');
    const observed = await api('GET', referencePath);
    if (observed.status !== 200) throw failure('Request workflow tag reservation conflict');
    const value = body(observed);
    if (value.ref !== 'refs/tags/' + ref || value.object?.sha !== commit.sha) throw failure('Request workflow tag reservation conflict');
  } else {
    const value = body(created);
    if (value.ref !== 'refs/tags/' + ref || value.object?.sha !== commit.sha) throw failure('Published request workflow identity changed');
  }
  return manifest;
}

/** Never remove a tag that no longer identifies our persisted caller commit. */
export async function deleteRequestWorkflow(api, request, workflow) {
  if (!workflow) return;
  assertRepository(request.repository);
  const manifest = normalizeWorkflowClaim(workflow, request.request_id);
  const root = '/repos/' + request.repository;
  const observed = await api('GET', root + '/git/ref/tags/' + manifest.ref);
  if (observed.status === 404) return;
  if (observed.status !== 200) throw failure('Could not verify request workflow cleanup identity');
  const value = body(observed);
  if (value.ref !== 'refs/tags/' + manifest.ref || value.object?.sha !== manifest.sha) {
    throw failure('Request workflow tag identity changed; existing work preserved');
  }
  const removed = await api('DELETE', root + '/git/refs/tags/' + manifest.ref);
  if (![204, 404].includes(removed.status)) throw failure('Request workflow tag cleanup failed');
}
