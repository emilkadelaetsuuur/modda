import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { spawn } from 'node:child_process';
import { readFile, stat, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const server = new McpServer({ name: 'modda', version: '1.1.0' });
const desktop = !process.env.MODDA_JOB_DIR;
const home = process.env.MODDA_HOME;
const session = randomUUID();
let current = process.env.MODDA_JOB_DIR;
const result = (value, isError = false) => ({
  isError, content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) }]
});

async function nextJob() {
  const pending = join(home, 'desktop-pending.json');
  try {
    const entry = JSON.parse(await readFile(pending, 'utf8'));
    if (!/^[a-f0-9]{24}$/.test(entry.id) || Date.now() / 1000 > entry.expires) return null;
    const root = join(home, 'jobs', entry.id);
    for (const marker of ['ready.json', 'attention.json', 'worker-error.json']) {
      if (await stat(join(root, marker)).catch(() => null)) return null;
    }
    return root;
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function task(op, args) {
  if (desktop && (!current || current !== await nextJob())) {
    return result('Вызовите next для получения текущего задания MODDA.', true);
  }
  return await new Promise(resolve => {
    const child = spawn(process.env.MODDA_PYTHON || 'python',
      ['-X', 'utf8', fileURLToPath(new URL('./job_tools.py', import.meta.url)), op],
      { cwd: current, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    const out = [], err = [];
    child.stdout.on('data', chunk => out.push(chunk));
    child.stderr.on('data', chunk => err.push(chunk));
    child.on('error', e => resolve(result(e.message, true)));
    child.on('close', code => resolve(result(Buffer.concat(out).toString().trim() ||
      Buffer.concat(err).toString().slice(-500) || 'No output', code !== 0)));
    child.stdin.end(JSON.stringify(args));
  });
}
if (desktop) {
  server.registerTool('next', {description:'Find the active, operator-selected desktop job. Call once per polling turn.',inputSchema:{}},
    async () => {
      const pulse = join(home, 'desktop-online');
      await writeFile(pulse, String(Date.now()));
      let job;
      for (let attempt = 0; attempt < 9; attempt++) {
        job = await nextJob();
        if (job) break;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
      if (!job) return result({pending:false});
      try {
        await writeFile(join(job, 'desktop-claim'), session, {flag:'wx'});
      } catch (error) {
        if (error.code === 'EEXIST') return result({pending:false, claimed:true});
        throw error;
      }
      current = job;
      return result({pending:true});
    });
}
function tool(name, description, inputSchema) {
  server.registerTool(name, { description, inputSchema }, args => task(name, args));
}
tool('brief', 'Read the order: duration budget, source passages, speakers, catalogs of camera shots and motions, style and rules.', {});
tool('scenario', 'Submit the full video script (clips for Flow, voiced shots with captions, facts with verbatim quotes). '
  + 'It is checked against the law and the ordered duration; on success returns the Flow frame and video prompts per clip.', {
  title: z.string().max(160),
  clips: z.array(z.object({ id: z.string().describe('c1, c2, ... in order'), shot: z.string().describe('key from shots_catalog'),
    characters: z.array(z.string()).describe('keys from on_screen_characters, may be empty'),
    scene: z.string().describe('one concrete place and situation, English'), props: z.array(z.string()).describe('1-5 concrete objects, English'),
    action: z.string().describe('one clear action for the 8-second video, English'), motion: z.string().describe('key from motions_catalog') })).min(1).max(8),
  facts: z.array(z.object({ source: z.number().int().describe('passage index'), claim: z.string(),
    quote: z.string().describe('exact verbatim quote from that passage') })).min(1).max(8),
  shots: z.array(z.object({ speaker: z.string().describe('key from speakers'), emotion: z.string(), text: z.string().describe('spoken line, numbers in words'),
    captions: z.array(z.array(z.string()).length(2)).describe('[[chunk, colour w|y|r|g], ...]'),
    clip: z.string(), offset: z.number().describe('0 for the first use of a clip, 4 for the second'),
    sfx: z.array(z.string()).optional(), facts: z.array(z.string()).optional().describe('f1, f2 ... in the order of facts') })).min(3).max(20)
});
tool('start_clip', 'Reserve the paid Flow video generation for a clip immediately before pressing generate. '
  + 'A retake of a defective clip (text, numbers, broken character) needs retake_reason.', {clip:z.string(), retake_reason:z.string().optional()});
tool('attach', 'Attach a Flow clip to a scene. Call it right after clicking Download on that clip, without filename: '
  + 'it waits up to 75 s and takes the newest download from Downloads even while the browser Save dialog is still open.', {clip:z.string(),filename:z.string().optional(),
  replace:z.boolean().optional().describe('true only to replace a clip with its authorized retake')});
tool('finish', 'Queue local MODDA montage once all the scenes have been attached.', {});
tool('attention', 'Record the blocker for the owner.', {message:z.string().min(1).max(420)});
await server.connect(new StdioServerTransport());
