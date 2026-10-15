import assert from 'node:assert/strict';
import {copyFileSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import test from 'node:test';

function fixture(t) {
    const root = mkdtempSync(path.join(tmpdir(), 'dootask frontend '));
    t.after(() => rmSync(root, {recursive: true, force: true}));
    copyFileSync(new URL('../cmd', import.meta.url), path.join(root, 'cmd'));
    const run = (...args) => spawnSync(process.execPath, [path.join(root, 'cmd'), ...args], {
        cwd: tmpdir(), encoding: 'utf8', env: {...process.env, BUILD_FRONTEND: 'stale'},
    });
    const stub = (relative, code) => {
        const file = path.join(root, relative);
        mkdirSync(path.dirname(file), {recursive: true});
        writeFileSync(file, code);
    };
    return {root, run, stub};
}

const report = `console.log(JSON.stringify({args: process.argv.slice(2), cwd: process.cwd(), mode: process.env.BUILD_FRONTEND}));`;

test('help works without dependencies or environment files', t => {
    const {root, run} = fixture(t);
    const result = run('help');
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /deploy\/README.md/);
    assert.equal(existsSync(path.join(root, '.env')), false);
});

test('obsolete backend commands fail without running a child', t => {
    const {run, stub} = fixture(t);
    stub('node_modules/vite/bin/vite.js', 'throw new Error("Unexpected child");');
    for (const command of ['install', 'update', 'uninstall', 'artisan', 'composer', 'php', 'mysql']) {
        const result = run(command);
        assert.equal(result.status, 2);
        assert.match(result.stderr, /Unsupported command/);
        assert.doesNotMatch(result.stderr, /Unexpected child/);
    }
});

test('missing dependencies produce an actionable error', t => {
    const {run} = fixture(t);
    assert.match(run('build').stderr, /npm ci/);
    assert.equal(run('build').status, 1);
});

test('build and dev aliases preserve arguments and use the frontend directory', t => {
    const {root, run, stub} = fixture(t);
    stub('node_modules/vite/bin/vite.js', report);
    for (const command of ['build', 'prod', 'dev', 'serve', 'start']) {
        const result = run(command, '--outDir', 'output with spaces');
        assert.equal(result.status, 0, result.stderr);
        const data = JSON.parse(result.stdout);
        assert.equal(data.cwd, root);
        assert.deepEqual(data.args, [
            ...(['build', 'prod'].includes(command) ? ['build'] : []),
            '--outDir', 'output with spaces',
        ]);
    }
});

test('a failed build returns its exit status', t => {
    const {run, stub} = fixture(t);
    stub('node_modules/vite/bin/vite.js', 'process.exit(7);');
    assert.equal(run('prod').status, 7);
});

test('Electron receives the selected frontend mode and original arguments', t => {
    const {root, run, stub} = fixture(t);
    stub('node_modules/vite/bin/vite.js', report);
    stub('electron/build.js', report);
    mkdirSync(path.join(root, 'electron/node_modules'));
    for (const args of [[], ['dev'], ['release', '1.2.3']]) {
        const result = run('electron', ...args);
        assert.equal(result.status, 0, result.stderr);
        const data = JSON.parse(result.stdout);
        assert.equal(data.mode, args[0] === 'dev' ? 'dev' : 'build');
        assert.equal(data.cwd, root);
        assert.deepEqual(data.args, args);
    }
});
