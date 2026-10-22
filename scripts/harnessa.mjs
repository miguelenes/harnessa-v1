import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const targets = {
  api: {
    directory: 'apps/api',
    commands: {
      dev: ['uv', 'run', '--project', root, 'uvicorn', 'harnessa.main:app', '--reload'],
      lint: ['pnpm', '--dir', root, 'run', 'lint:api'],
      typecheck: ['pnpm', '--dir', root, 'run', 'typecheck:api'],
      test: ['pnpm', '--dir', root, 'run', 'test:api'],
      integration: ['pnpm', '--dir', root, 'run', 'test:integration'],
    },
  },
  web: { directory: 'apps/web', commands: { dev: 'dev', build: 'build', lint: 'lint', typecheck: 'typecheck' } },
  marketing: { directory: 'apps/marketing', commands: { dev: 'dev', build: 'build', preview: 'preview' } },
  'cloud-portal': {
    directory: 'apps/cloud-portal',
    commands: { dev: 'dev', build: 'build', preview: 'preview', check: 'check' },
  },
  'ui-react': { directory: 'packages/ui-react', commands: { build: 'build', typecheck: 'typecheck' } },
  'ui-svelte': { directory: 'packages/ui-svelte', commands: { build: 'build', typecheck: 'typecheck' } },
  contracts: { directory: 'packages/contracts', commands: { generate: 'generate', typecheck: 'typecheck' } },
};

function usage() {
  console.log('Usage: pnpm harnessa list | pnpm harnessa <target> <command>');
  for (const [name, target] of Object.entries(targets)) {
    console.log(`  ${name}: ${Object.keys(target.commands).join(', ')}`);
  }
}

const [, , targetName, commandName] = process.argv;
if (targetName === 'list' && commandName === undefined) {
  usage();
  process.exit(0);
}

const target = Object.hasOwn(targets, targetName) ? targets[targetName] : undefined;
if (!target || !commandName || !Object.hasOwn(target.commands, commandName)) {
  usage();
  process.exit(2);
}

const configured = target.commands[commandName];
const args = Array.isArray(configured) ? configured : ['pnpm', 'run', configured];
const [executable, ...executableArgs] = args;
const result = spawnSync(executable, executableArgs, {
  cwd: path.join(root, target.directory),
  stdio: 'inherit',
  env: process.env,
});
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
