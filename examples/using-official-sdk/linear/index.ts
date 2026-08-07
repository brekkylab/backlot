/**
 * Read Linear issues from an enterprise-mock server with the OFFICIAL `@linear/sdk`.
 *
 * This is the only TypeScript example in `examples/using-official-sdk/` — every sibling is
 * Python — because `@linear/sdk` is the only client Linear publishes and there is no official
 * Python SDK at all. A README snippet would have been cheaper, but an example nobody runs cannot
 * back the claim that the mock works with the real client, so this one runs (and CI runs it).
 *
 * Two things make it non-obvious, both handled below:
 *
 *  1. `LinearClient` has NO base-URL option. It accepts `apiKey`, `accessToken` and a
 *     `RequestInit`, and nothing else — so pointing it at a mock means using Linear's own
 *     documented escape hatch: extend `LinearSdk` with a custom request function.
 *  2. The SDK's own generated documents are enormous (`client.issues()` selects 171 field nodes
 *     across 11 fragments). The mock's schema is generated from those very documents, which is
 *     why every field resolves instead of the query being rejected outright.
 *
 * Self-contained, like the Python examples: with no reachable `--url` it imports a tiny corpus
 * into a throwaway DB and runs the mock itself.
 *
 *     npm install && npx tsx index.ts
 *     npx tsx index.ts --url http://localhost:8000
 *     npx tsx index.ts --url http://localhost:8000 --token <usr-token>
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { LinearSdk, parseLinearError, type LinearRequest } from "@linear/sdk";
import { GraphQLClient } from "graphql-request";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..", "..");
/** The Settings default; a per-user token lives in <data>/tokens.yaml (and GET /_mock/users). */
const ADMIN_TOKEN = "admin-service-token";

/** The same shape the Python examples' in-code corpora use — see `_mockserver.py`. */
const CORPUS = [
  {
    source_type: "linear", doc_id: "lin-kv", team: "engineering", group: "engineering",
    title: "Variant-aware GPU allocation and KV residency",
    content: "Long-context configs push peak GPU memory into fragile regions.",
    author_email: "amaya.chen@acme.com", author_groups: ["engineering"], visibility: "public",
    identifier: "ENG-49121", state: "In Progress", priority: "P1", estimate: 5,
    labels: ["kv-cache", "memory-optimization"], project: "runtime-memory-2025",
    cycle: "2025-W08", dueDate: "2025-03-15",
    assignee: "diego.martinez@acme.com", assigneeName: "Diego Martinez",
    created: "2025-02-18T00:00:00Z", updated: "2025-03-04T00:00:00Z",
    comments: [
      { content: "Baseline traces captured; peak 98.2 GB on the quantized variant.",
        author_email: "amaya.chen@acme.com" },
      { content: "Residency bands cut peak memory 20%.", author_email: "diego.martinez@acme.com" },
    ],
  },
  {
    source_type: "linear", doc_id: "lin-batch", team: "engineering", group: "engineering",
    title: "Continuous batching stalls after compaction",
    content: "A 50ms stall when the batcher merges requests right after a compaction pass.",
    author_email: "diego.martinez@acme.com", author_groups: ["engineering"], visibility: "public",
    identifier: "ENG-49188", state: "Done", priority: "P0", estimate: 3,
    labels: ["batching", "latency"], project: "runtime-memory-2025",
    created: "2025-03-01T00:00:00Z", updated: "2025-03-10T00:00:00Z",
  },
  {
    source_type: "linear", doc_id: "lin-des", team: "design", group: "design",
    title: "Model chooser inline composer",
    content: "Compact variants for the inline model picker.",
    author_email: "maya.chen@acme.com", author_groups: ["design"], visibility: "public",
    identifier: "DES-128743", state: "In Review", priority: "P2", labels: ["composer"],
  },
];

// ---------------------------------------------------------------- the shim
/**
 * `LinearClient` cannot be pointed anywhere but api.linear.app, so subclass `LinearSdk` — the
 * generated query layer underneath the client — with a request function of our own. Linear's
 * advanced-usage docs describe exactly this pattern.
 *
 * `GraphQLClient.request` already has `LinearRequest`'s shape, so the shim is three lines. The
 * only wrinkle is the document type: the SDK hands over a `TypedDocumentString` (a class with a
 * `toString()`), not a plain string, so it is stringified before it goes on the wire.
 */
class MockLinearClient extends LinearSdk {
  constructor(baseUrl: string, apiKey: string) {
    const gql = new GraphQLClient(`${baseUrl.replace(/\/$/, "")}/linear/graphql`, {
      // A Linear personal API key is the BARE header value — no `Bearer` prefix. (The mock
      // accepts `Bearer <token>` too, which is how Linear carries an OAuth access token.)
      headers: { Authorization: apiKey },
    });
    const request: LinearRequest = (document, variables) =>
      gql
        .request(String(document), variables ?? {})
        .catch((error) => {
          throw parseLinearError(error);
        }) as any;
    super(request);
  }
}

// ---------------------------------------------------------------- mock plumbing
function freePort(): Promise<number> {
  return new Promise((res, rej) => {
    const srv = createServer();
    srv.on("error", rej);
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as { port: number }).port;
      srv.close(() => res(port));
    });
  });
}

async function healthy(url: string, timeoutMs = 10_000): Promise<boolean> {
  try {
    const res = await fetch(`${url.replace(/\/$/, "")}/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function waitForHealth(url: string, tries = 100): Promise<void> {
  for (let i = 0; i < tries; i++) {
    if (await healthy(url, 500)) return;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error("mock server did not become ready");
}

interface Mock {
  baseUrl: string;
  token: string;
  stop(): void;
}

/**
 * Use `--url` if it answers; otherwise build a throwaway DB from CORPUS and run uvicorn against
 * it — the same fallback `_mockserver.py` gives every Python example, so this script needs no
 * separate process launched by hand.
 */
async function serveOrConnect(url: string | undefined): Promise<Mock> {
  if (url && (await healthy(url))) {
    console.log(`using mock server at ${url}`);
    return { baseUrl: url.replace(/\/$/, ""), token: ADMIN_TOKEN, stop: () => {} };
  }
  if (url) console.log(`--url ${url} is not reachable — falling back to a local mock`);

  const python = process.env.PYTHON ?? "python3";
  const dataDir = mkdtempSync(join(tmpdir(), "enterprise-mock-linear-"));
  const corpus = join(dataDir, "corpus.jsonl");
  writeFileSync(corpus, CORPUS.map((r) => JSON.stringify(r)).join("\n"));
  const env = { ...process.env, BACKLOT_DATA_DIR: dataDir };

  const imported = spawnSync(python, ["-m", "backlot.importer.byo", corpus], {
    cwd: REPO_ROOT, env, stdio: ["ignore", "ignore", "inherit"],
  });
  if (imported.status !== 0) {
    rmSync(dataDir, { recursive: true, force: true });
    throw new Error(
      `could not build the throwaway corpus with ${python}. Install the package ` +
        `(pip install -e .) or point at a running mock with --url.`,
    );
  }

  const port = await freePort();
  const proc: ChildProcess = spawn(
    python,
    ["-m", "uvicorn", "backlot.main:app", "--port", String(port), "--log-level", "warning"],
    { cwd: REPO_ROOT, env, stdio: ["ignore", "ignore", "inherit"] },
  );
  const baseUrl = `http://127.0.0.1:${port}`;
  const stop = () => {
    proc.kill();
    rmSync(dataDir, { recursive: true, force: true });
  };
  try {
    await waitForHealth(baseUrl);
  } catch (e) {
    stop();
    throw e;
  }
  return { baseUrl, token: ADMIN_TOKEN, stop };
}

// ---------------------------------------------------------------- the example
async function main(client: MockLinearClient): Promise<void> {
  // `viewer` confirms which identity the credential resolved to — with `--token` that is a real
  // person and everything below is ACL-filtered to what they can see.
  const me = await client.viewer;
  console.log(`authenticated as ${me.name} <${me.email}>`);

  // Every call below goes through the SDK's OWN generated document, not a hand-written query.
  const teams = await client.teams({ first: 10 });
  console.log(`\nteams (${teams.nodes.length}):`);
  for (const team of teams.nodes) {
    console.log(`  ${team.key.padEnd(5)} ${team.name} — ${team.issueCount} issue(s)`);
  }

  const issues = await client.issues({ first: 5 });
  console.log(`\nissues (${issues.nodes.length}, hasNextPage=${issues.pageInfo.hasNextPage}):`);
  for (const issue of issues.nodes) {
    // Relations are lazy in the SDK: each of these awaits its own follow-up query.
    const [state, assignee, project, labels] = await Promise.all([
      issue.state, issue.assignee, issue.project, issue.labels(),
    ]);
    console.log(
      `  ${issue.identifier.padEnd(11)} ${issue.title}\n` +
        `      state=${state?.name} (${state?.type})  priority=${issue.priorityLabel}` +
        `  estimate=${issue.estimate ?? "—"}\n` +
        `      assignee=${assignee?.name ?? "unassigned"}  project=${project?.name ?? "—"}` +
        `  labels=[${labels.nodes.map((l) => l.name).join(", ")}]\n` +
        `      branch=${issue.branchName}`,
    );
  }

  // A team's issues, which is the shape the LlamaIndex reader also uses (`data.team.issues`).
  const first = teams.nodes[0];
  if (first) {
    const teamIssues = await first.issues({ first: 3 });
    console.log(`\n${first.key} issues via team.issues(): ` +
      teamIssues.nodes.map((i) => i.identifier).join(", "));
  }

  // Comments hang off an issue as their own connection. Pick one that actually has some, so the
  // section shows the shape rather than an empty list.
  for (const issue of issues.nodes) {
    const comments = await issue.comments();
    if (comments.nodes.length === 0) continue;
    console.log(`\ncomments on ${issue.identifier} (${comments.nodes.length}):`);
    for (const c of comments.nodes) {
      const user = await c.user;
      console.log(`  ${user?.name ?? "unknown"}: ${c.body}`);
    }
    break;
  }

  // Server-side filtering — the mock compiles this into SQL rather than filtering in memory.
  const urgent = await client.issues({ filter: { priority: { lte: 2 } }, first: 10 });
  console.log(`\nissues with priority <= 2 (High/Urgent): ` +
    urgent.nodes.map((i) => `${i.identifier} (${i.priorityLabel})`).join(", "));

  // Cursor pagination: page 1 then page 2 through `pageInfo.endCursor`.
  const page1 = await client.issues({ first: 1 });
  const page2 = await client.issues({ first: 1, after: page1.pageInfo.endCursor });
  console.log(`\npaging: page1=${page1.nodes[0]?.identifier} ` +
    `page2=${page2.nodes[0]?.identifier} hasNextPage=${page2.pageInfo.hasNextPage}`);
}

function parseArgs(): { url?: string; token?: string } {
  const argv = process.argv.slice(2);
  const out: { url?: string; token?: string } = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--url") out.url = argv[++i];
    else if (argv[i] === "--token") out.token = argv[++i];
    else throw new Error(`unknown argument ${argv[i]} (expected --url / --token)`);
  }
  return out;
}

const args = parseArgs();
const mock = await serveOrConnect(args.url);
try {
  if (args.token) console.log("authenticating with --token → responses are ACL-filtered to that user");
  await main(new MockLinearClient(mock.baseUrl, args.token ?? mock.token));
} finally {
  mock.stop();
}
