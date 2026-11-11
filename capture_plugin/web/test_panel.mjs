import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { parse } from 'acorn';

const source = readFileSync(new URL('./panel.js', import.meta.url), 'utf8');

test('Capture AMD loader does not capture the global preview and icon libraries', () => {
    const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
    const scripts = Array.from(html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g), (match) => match[1]);
    const amdCalls = [];
    const context = vm.createContext({ console });
    context.window = context;
    context.self = context;
    for (const path of scripts) {
        if (path.endsWith('/dojo/dojo/dojo.js')) {
            context.define = (dependencies) => amdCalls.push(dependencies);
            context.define.amd = {};
        } else if (path.startsWith('__PLUGIN_ASSETS__/node_modules/')) {
            const relative = path.replace('__PLUGIN_ASSETS__/', './');
            vm.runInContext(readFileSync(new URL(relative, import.meta.url), 'utf8'), context, { filename: relative });
        }
    }
    assert.equal(amdCalls.length, 0, 'UMD scripts must not register anonymous Dojo modules');
    assert.equal(typeof context.THREE.GLTFLoader, 'function');
    assert.equal(typeof context.THREE.OrbitControls, 'function');
    assert.equal(typeof context.lucide.createIcons, 'function');
    assert.ok(scripts.findIndex((path) => path.endsWith('/dojo/dojo/dojo.js'))
        < scripts.findIndex((path) => path.endsWith('/OrDzFramework.js')));
});

test('panel and preview dependencies parse in Capture Chromium 70', () => {
    const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
    const paths = Array.from(html.matchAll(/<script\b[^>]*\bsrc="(__PLUGIN_ASSETS__\/[^"]+)"/g),
        (match) => match[1].replace('__PLUGIN_ASSETS__/', './'));
    for (const path of paths) {
        assert.doesNotThrow(() => parse(readFileSync(new URL(path, import.meta.url), 'utf8'),
            { ecmaVersion: 2018, sourceType: 'script' }), path);
    }
});

test('compatible Lucide exports every icon referenced by the panel', () => {
    const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
    const context = vm.createContext({ console });
    vm.runInContext(readFileSync(new URL('./node_modules/lucide/dist/umd/lucide.min.js', import.meta.url), 'utf8'), context);
    for (const [, name] of html.matchAll(/data-lucide="([^"]+)"/g)) {
        const exported = name.replace(/(^|-)([a-z])/g, (_, separator, letter) => letter.toUpperCase());
        assert.ok(context.lucide.icons[exported], `${name} is available`);
    }
    assert.match(source, /createIcons\(\{nameAttr: 'data-lucide'\}\)/);
});

function panel(options = {}) {
    const requests = [];
    const threeCalls = [];
    const handlers = {};
    const timers = new Map();
    let clock = 0;
    let timerId = 0;
    let serverAccesses = 0;
    let documentMode = 'local';
    class Element {
        constructor(id = '') {
            this.id = id;
            this.children = [];
            this.value = '';
            this.checked = false;
            this.disabled = false;
            this.hidden = false;
            this.attributes = {};
            this.draws = [];
            this._text = '';
        }
        set textContent(value) { this._text = String(value); this.children = []; }
        get textContent() { return this._text; }
        appendChild(child) { this.children.push(child); return child; }
        setAttribute(key, value) { this.attributes[key] = String(value); }
        getAttribute(key) { return this.attributes[key] ?? null; }
        focus() { this.focused = true; }
        removeAttribute(key) { delete this.attributes[key]; if (key === 'src') { this.src = ''; } }
        getBoundingClientRect() { return { width: 300, height: 180 }; }
        getContext() {
            if (!this.context) {
                this.context = new Proxy({}, {
                    get: (target, key) => target[key] ?? ((...args) => this.draws.push({ method: key, args })),
                    set: (target, key, value) => { target[key] = value; return true; },
                });
            }
            return this.context;
        }
    }
    const elements = new Map();
    const element = (id) => {
        if (!elements.has(id)) { elements.set(id, new Element(id)); }
        return elements.get(id);
    };
    const context = {
        document: { getElementById: element, createElement: () => new Element(), querySelectorAll: () => [], addEventListener(kind, handler) { handlers[kind] = handler; } },
        devicePixelRatio: 1,
        matchMedia: () => ({ matches: false }),
        addEventListener() {},
        btoa: (value) => Buffer.from(value, 'latin1').toString('base64'),
        atob: (value) => Buffer.from(value, 'base64').toString('latin1'),
        encodeURIComponent, decodeURIComponent, escape, unescape,
        Date: { now: () => clock },
        setTimeout(callback, delay) { timers.set(++timerId, { callback, time: clock + delay }); return timerId; },
        clearTimeout(id) { timers.delete(id); },
        external: options.bridgeReady === false ? {} : { orPrmWidgetId: 101, orPrmConnector() {} },
        JLC3D: {
            clear() { threeCalls.push({ kind: 'clear' }); element('step-preview').hidden = true; },
            show(data, failed) {
                threeCalls.push({ kind: 'show', data });
                if (options.stepError) {
                    if (options.stepErrorDelay) { context.setTimeout(() => failed(options.stepError), options.stepErrorDelay); }
                    else { failed(options.stepError); }
                    return;
                }
                element('step-preview').hidden = false;
            },
            expand(container) { threeCalls.push({ kind: 'expand', target: container.id }); return !element('step-preview').hidden; },
            restore() { threeCalls.push({ kind: 'restore' }); },
        },
        OrDzServerInterface: {
            setDocumentMode(mode) { documentMode = mode; },
            sGetServerInterface: () => {
                serverAccesses += 1;
                if (context.external.orPrmWidgetId == null) { documentMode = 'server'; }
                return {
                nativeCall(_method, command, callback) {
                    const [, kind, encoded] = command.split(' ');
                    requests.push({ kind, data: JSON.parse(Buffer.from(encoded, 'base64').toString('utf8')) });
                    callback('queued');
                    if (kind === 'init' && options.autoInit !== false) {
                        context.JLC2Dock.receive('init', Buffer.from(JSON.stringify({ library_root: 'D:/test-library' }), 'utf8').toString('base64'));
                    }
                },
                };
            },
        },
    };
    context.window = context;
    element('zoom').hidden = true;
    vm.runInNewContext(source, context, { filename: 'panel.js' });
    return {
        element, requests, threeCalls,
        bridgeReady() { context.external.orPrmWidgetId = 101; context.external.orPrmConnector = function () {}; },
        serverAccesses: () => serverAccesses,
        documentMode: () => documentMode,
        advance(milliseconds) {
            const target = clock + milliseconds;
            while (true) {
                const next = [...timers].sort((a, b) => a[1].time - b[1].time)[0];
                if (!next || next[1].time > target) { break; }
                timers.delete(next[0]); clock = next[1].time; next[1].callback();
            }
            clock = target;
        },
        keydown(key) { handlers.keydown({ key, preventDefault() {} }); },
        submit(query) {
            element('query').value = query;
            element('search-form').onsubmit({ preventDefault() {} });
        },
        receive(kind, data, requestId) {
            context.JLC2Dock.receive(kind, Buffer.from(JSON.stringify(data), 'utf8').toString('base64'), requestId);
        },
    };
}

test('a late native bridge cannot lock the framework into server mode or accept early searches', () => {
    const app = panel({ bridgeReady: false });
    assert.equal(app.serverAccesses(), 0);
    assert.equal(app.element('search').disabled, true);
    app.submit('early');
    app.advance(750);
    assert.equal(app.serverAccesses(), 0);
    app.bridgeReady();
    app.advance(100);
    assert.equal(app.documentMode(), 'local');
    assert.deepEqual(app.requests.map((item) => item.kind), ['init']);
    assert.equal(app.element('library-root').textContent, 'D:/test-library');
    assert.equal(app.element('search').disabled, false);
    app.advance(20000);
    assert.equal(app.requests.length, 1);
});

test('a missing native bridge times out visibly and a single reconnect can recover', () => {
    const app = panel({ bridgeReady: false });
    app.advance(10000);
    assert.match(app.element('connection').textContent, /\u672a\u8fde\u63a5/);
    assert.match(app.element('status').textContent, /\u8d85\u65f6/);
    assert.equal(app.element('reconnect').hidden, false);
    assert.equal(app.serverAccesses(), 0);
    app.bridgeReady();
    app.element('reconnect').onclick();
    app.element('reconnect').onclick();
    app.advance(200);
    assert.deepEqual(app.requests.map((item) => item.kind), ['init']);
    assert.equal(app.element('reconnect').hidden, true);
    assert.equal(app.element('search').disabled, false);
});

test('an init acknowledgement without its payload times out without retry bursts', () => {
    const app = panel({ autoInit: false });
    assert.equal(app.element('search').disabled, true);
    app.advance(10000);
    assert.deepEqual(app.requests.map((item) => item.kind), ['init']);
    assert.match(app.element('status').textContent, /\u8d85\u65f6/);
    app.receive('init', { library_root: 'D:/late-library' });
    assert.equal(app.element('search').disabled, false);
    assert.equal(app.element('library-root').textContent, 'D:/late-library');
    app.advance(20000);
    assert.equal(app.requests.length, 1);
});

const rows = [
    { lcsc_id: 'C100', title: 'Part 100', manufacturer: 'Maker A', package: 'SOIC', stock: null },
    { lcsc_id: 'C200', title: 'Part 200', manufacturer: 'Maker B', package: 'QFN', stock: 0 },
    { lcsc_id: 'C300', title: 'Part 300', manufacturer: 'Maker A', package: 'QFN', stock: 25 },
];

function searchResult(overrides = {}) {
    return { query: 'resistor', results: rows, page: 1, page_size: 30, total: 61, has_more: true, warnings: [], ...overrides };
}

function loadRows(app) {
    app.submit('resistor');
    app.receive('search', searchResult());
}

function choose(app, id) {
    const row = app.element('results').children.find((item) => item.children[0].textContent === id);
    assert.ok(row, `visible row ${id}`);
    row.onclick();
}

function preview(part = 'C100') {
    return {
        part_id: part, footprint_name: `${part}_SOIC`, step_status: 'missing', warnings: [],
        preview_data: {
            symbol: {
                pins: [
                    { number: '1', name: 'IN', x: -10, y: -30, end_x: 0, end_y: -30 },
                    { number: '2', name: 'OUT', x: 90, y: -30, end_x: 80, end_y: -30 },
                ],
                graphics: [{ kind: 'line', width: 0, points: [[0, 0], [80, 0], [80, -40], [0, -40], [0, 0]] }],
            },
            footprint: { graphics: [], pads: [{ number: '1', x: 5, y: 0, size: [2, 1], rotation: 45, shape: 'rect' }] },
        },
        step_files: [], step_viewer_url: '',
    };
}

test('search lists results without selecting or downloading any part', () => {
    const app = panel();
    loadRows(app);
    assert.deepEqual(app.requests.map((item) => item.kind), ['init', 'search']);
    assert.equal(app.element('results').children.length, 3);
    assert.equal(app.element('import').disabled, true);
    assert.equal(app.element('place').disabled, true);
    assert.equal(app.element('step-preview').hidden, true);
    assert.equal(app.threeCalls.filter((call) => call.kind === 'show').length, 0);
});

test('pagination retains the submitted query while the input is being edited', () => {
    const app = panel();
    loadRows(app);
    app.element('query').value = 'op amp';
    app.element('next').onclick();
    assert.deepEqual(app.requests.at(-1), { kind: 'search', data: { query: 'resistor', page: 2, request_id: 2 } });
});

test('page navigation reuses a recent in-panel search result without starting a worker', () => {
    const app = panel();
    loadRows(app);
    const before = app.requests.length;
    app.element('next').onclick();
    assert.equal(app.requests.length, before + 1);
    const pageTwoId = app.requests.at(-1).data.request_id;
    app.receive('search', searchResult({ page: 2, results: [rows[2]], total: 61, has_more: false }), pageTwoId);
    assert.equal(app.element('page').textContent, '2');
    app.element('previous').onclick();
    assert.equal(app.requests.length, before + 1);
    assert.equal(app.element('page').textContent, '1');
    assert.equal(app.element('results').children.length, 3);
});

test('explicit submit starts the edited query at page one', () => {
    const app = panel();
    loadRows(app);
    app.submit('op amp');
    assert.deepEqual(app.requests.at(-1), { kind: 'search', data: { query: 'op amp', page: 1, request_id: 2 } });
});

test('manufacturer, package and stock filters compose without treating unknown stock as available', () => {
    const app = panel();
    loadRows(app);
    assert.equal(app.element('results').children[0].children[4].textContent, '\u2014');
    app.element('manufacturer').value = 'Maker A';
    app.element('manufacturer').onchange();
    assert.equal(app.element('results').children.length, 2);
    app.element('package').value = 'QFN';
    app.element('package').onchange();
    app.element('in-stock').checked = true;
    app.element('in-stock').onchange();
    assert.deepEqual(app.element('results').children.map((row) => row.children[0].textContent), ['C300']);
    assert.deepEqual(app.requests.map((item) => item.kind), ['init', 'search']);
});

test('preview keeps browsing responsive while native import and placement stay disabled', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    assert.deepEqual(app.requests.at(-1), { kind: 'preview', data: { part: 'C100', request_id: 2 } });
    assert.equal(app.element('progress').hidden, false);
    for (const id of ['import', 'place', 'view-step']) {
        assert.equal(app.element(id).disabled, true, `${id} disabled during preview`);
    }
    const count = app.requests.length;
    app.element('import').onclick();
    app.element('place').onclick();
    assert.equal(app.requests.length, count);
    for (const id of ['search', 'manufacturer', 'package', 'in-stock']) { assert.equal(app.element(id).disabled, false); }
    app.receive('preview', preview());
    assert.equal(app.element('progress').hidden, true);
    assert.equal(app.element('place').disabled, false);
    app.element('import').onclick();
    assert.deepEqual(app.requests.at(-1), { kind: 'import', data: { part: 'C100', request_id: 3 } });
});

test('new selection supersedes a pending preview and its late error cannot clear the new state', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    const oldId = app.requests.at(-1).data.request_id;
    choose(app, 'C200');
    const currentId = app.requests.at(-1).data.request_id;
    assert.deepEqual(app.requests.at(-1), {kind: 'preview', data: {part: 'C200', request_id: currentId}});
    app.receive('preview', preview('C100'), oldId);
    app.receive('error', {message: 'old failure'}, oldId);
    assert.equal(app.element('part-id').textContent, 'C200');
    assert.equal(app.element('progress').hidden, false);
    app.receive('preview', preview('C200'), currentId);
    assert.equal(app.element('progress').hidden, true);
});

test('3D download is explicit and changing selection supersedes it', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', {...preview(), step_status: 'deferred'});
    assert.equal(app.element('load-step').hidden, false);
    assert.equal(app.requests.at(-1).kind, 'preview');
    app.element('load-step').onclick();
    const oldId = app.requests.at(-1).data.request_id;
    assert.deepEqual(app.requests.at(-1), {kind: 'preview3d', data: {part: 'C100', request_id: oldId}});
    choose(app, 'C200');
    app.receive('preview3d', {...preview(), preview_3d: {format: 'glb'}}, oldId);
    assert.equal(app.element('part-id').textContent, 'C200');
    assert.equal(app.threeCalls.filter((call) => call.kind === 'show').length, 0);
});

test('new search can supersede a pending preview', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    const oldId = app.requests.at(-1).data.request_id;
    app.submit('new query');
    assert.equal(app.requests.at(-1).kind, 'search');
    app.receive('preview', preview(), oldId);
    assert.equal(app.element('progress').hidden, false);
    app.receive('search', searchResult({query: 'new query'}));
    assert.equal(app.element('progress').hidden, true);
});

test('asynchronous library path warning does not finish or unlock an active import', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', preview());
    app.element('import').onclick();
    app.receive('paths', {ok: false, message: 'Library path unavailable'});
    assert.equal(app.element('status').textContent, 'Library path unavailable');
    assert.equal(app.element('progress').hidden, false);
    assert.equal(app.element('place').disabled, true);
    app.receive('import', preview());
    assert.equal(app.element('progress').hidden, true);
});

test('source failures display Chinese import messages and release the pending operation', () => {
    for (const message of ['\u6ca1\u6709 CAD \u6587\u4ef6\uff1a\u672a\u627e\u5230\u8be5\u5143\u5668\u4ef6\u7684\u7acb\u521b\u539f\u59cb CAD \u5e93\uff0c\u65e0\u6cd5\u5bfc\u5165\u3002', '\u7531\u4e8e\u662f\u5f02\u5f62\u7ed3\u6784\uff0c\u5f53\u524d\u65e0\u6cd5\u751f\u6210\u3002']) {
        const app = panel();
        loadRows(app);
        choose(app, 'C100');
        app.receive('preview', preview());
        app.element('import').onclick();
        const count = app.requests.length;
        const requestId = app.requests.at(-1).data.request_id;
        app.receive('error', { user_message: message, error: 'original technical details', message: 'legacy detail' }, requestId);
        assert.equal(app.element('status').textContent, message);
        assert.equal(app.element('status').className, 'error');
        assert.equal(app.element('progress').hidden, true);
        assert.equal(app.element('import').disabled, false);
        assert.equal(app.requests.length, count, 'a failed import never triggers placement');
    }
});

test('extension updates use a fixed native action and are disabled during imports', () => {
    const app = panel();
    app.element('updates').onclick();
    assert.deepEqual(app.requests.at(-1), { kind: 'updates', data: {} });
    loadRows(app);
    choose(app, 'C100');
    assert.equal(app.element('updates').disabled, true);
    const count = app.requests.length;
    app.element('updates').onclick();
    assert.equal(app.requests.length, count);
    app.receive('preview', preview());
    assert.equal(app.element('updates').disabled, false);
});

test('dock source messages and unclassified errors keep their original explanation', () => {
    for (const data of [
        { message: '\u7531\u4e8e\u662f\u5f02\u5f62\u7ed3\u6784\uff0c\u5f53\u524d\u65e0\u6cd5\u751f\u6210\u3002' },
        { user_message: ' ', message: 'Catalog request failed: HTTP 403' },
        { error: 'Incomplete source electrical type' },
    ]) {
        const app = panel();
        loadRows(app);
        choose(app, 'C100');
        app.receive('error', data, app.requests.at(-1).data.request_id);
        assert.equal(app.element('status').textContent, data.message || data.error);
        assert.equal(app.element('progress').hidden, true);
    }
});

test('filtering a selected row out clears its preview and disables part actions', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', preview());
    app.element('manufacturer').value = 'Maker B';
    app.element('manufacturer').onchange();
    assert.equal(app.element('import').disabled, true);
    assert.equal(app.element('place').disabled, true);
    assert.equal(app.element('step-preview').hidden, true);
});

test('filtering out a loading selection cancels its UI wait and rejects its late response', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    const oldId = app.requests.at(-1).data.request_id;
    app.element('manufacturer').value = 'Maker B';
    app.element('manufacturer').onchange();
    assert.equal(app.element('progress').hidden, true);
    assert.equal(app.element('import').disabled, true);
    app.receive('preview', preview(), oldId);
    assert.equal(app.element('import').disabled, true);
    choose(app, 'C200');
    assert.equal(app.requests.at(-1).data.part, 'C200');
});

test('a preview for another part cannot release the active request busy state', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', preview('C200'));
    assert.equal(app.element('progress').hidden, false);
    assert.equal(app.element('place').disabled, true);
    app.receive('preview', preview('C100'));
    assert.equal(app.element('place').disabled, false);
});

test('a stale search response cannot overwrite a pending selected part preview', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('search', searchResult({ results: [rows[2]], page: 2 }));
    assert.equal(app.element('part-id').textContent, 'C100');
    assert.equal(app.element('progress').hidden, false);
    app.receive('preview', preview());
    assert.equal(app.element('place').disabled, false);
});

test('a response for a different query cannot finish the active search', () => {
    const app = panel();
    app.submit('resistor');
    app.receive('search', searchResult({ query: 'capacitor', results: [rows[2]] }));
    assert.equal(app.element('progress').hidden, false);
    assert.equal(app.element('results').children.length, 0);
    app.receive('search', searchResult());
    assert.equal(app.element('progress').hidden, true);
    assert.equal(app.element('results').children.length, 3);
});

test('a response of a different operation cannot finish the active preview', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('import', preview());
    assert.equal(app.element('progress').hidden, false);
    assert.equal(app.element('place').disabled, true);
    app.receive('preview', preview());
    assert.equal(app.element('place').disabled, false);
});

test('an earlier response of the same kind and query is rejected by request identity', () => {
    const app = panel();
    loadRows(app);
    const oldId = app.requests.at(-1).data.request_id;
    app.submit('resistor');
    const currentId = app.requests.at(-1).data.request_id;
    assert.notEqual(currentId, oldId);
    app.receive('search', searchResult({ results: [rows[2]] }), oldId);
    assert.equal(app.element('progress').hidden, false);
    assert.equal(app.element('results').children.length, 3);
    app.receive('search', searchResult({ results: [rows[1]] }), currentId);
    assert.equal(app.element('progress').hidden, true);
    assert.equal(app.element('results').children[0].children[0].textContent, 'C200');
});

test('symbol geometry is fitted using its coordinates when backend bounds are omitted', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', preview());
    const paths = app.element('symbol').draws.filter((draw) => ['moveTo', 'lineTo'].includes(draw.method));
    assert.ok(paths.length > 0);
    for (const draw of paths) {
        const [x, y] = draw.args;
        assert.ok(Number.isFinite(x) && Number.isFinite(y) && x >= 0 && x <= 300 && y >= 0 && y <= 180,
            `${draw.method}(${x}, ${y}) must fit inside the symbol canvas`);
    }
});

test('Capture XML display text is rendered even when a text graphic has no polyline points', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    const data = preview();
    data.preview_data.symbol.graphics.push({ kind: 'text', text: 'Actual symbol value', points: [], x: 10, y: -50 });
    app.receive('preview', data);
    assert.ok(app.element('symbol').draws.some((draw) => draw.method === 'fillText' && draw.args[0] === 'Actual symbol value'));
});

test('source labels replace automatic labels and source curves are drawn', () => {
    const app = panel();
    loadRows(app); choose(app, 'C100');
    const data = preview();
    data.preview_data.symbol.pins = [{number: '1', name: 'NO_AUTOLABEL', x: -10, y: 0, end_x: 0, end_y: 0, labels_in_graphics: true}];
    data.preview_data.symbol.graphics = [
        {kind: 'circle', x: 0, y: 0, points: [], properties: {radius_mm: '5'}},
        {kind: 'text', text: 'SOURCE_LABEL', x: 0, y: 2, points: [], properties: {font_size_mm: '1'}},
    ];
    app.receive('preview', data);
    assert.ok(app.element('symbol').draws.some(draw => draw.method === 'arc'));
    const labels = app.element('symbol').draws.filter(draw => draw.method === 'fillText').map(draw => draw.args[0]);
    assert.deepEqual(labels, ['SOURCE_LABEL']);
});

test('oblong source pads use semicircular ends instead of elliptical pads', () => {
    const app = panel();
    loadRows(app); choose(app, 'C100');
    const data = preview();
    data.preview_data.footprint = {graphics: [], pads: [{number: '1', x: 0, y: 0, size: [.4, 1.7], shape: 'oval'}]};
    app.receive('preview', data);
    assert.equal(app.element('footprint').draws.filter(draw => draw.method === 'arc').length, 2);
    assert.equal(app.element('footprint').draws.filter(draw => draw.method === 'ellipse').length, 0);
});

test('3D preview routes only GLB payloads to JLC3D and never embeds an external viewer URL', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', { ...preview(), step_viewer_url: 'https://example.com/view' });
    assert.equal(app.element('step-preview').hidden, true);
    assert.equal(app.threeCalls.filter((call) => call.kind === 'show').length, 0);
    choose(app, 'C100');
    const mesh = { format: 'glb', data_base64: 'Z2xURgIAAAA=' };
    app.receive('preview', { ...preview(), preview_3d: mesh, step_viewer_url: 'https://example.com/view' });
    const calls = app.threeCalls.filter((call) => call.kind === 'show');
    assert.equal(calls.length, 1);
    assert.equal(JSON.stringify(calls[0].data), JSON.stringify(mesh));
    assert.equal(app.element('step-preview').hidden, false);
    assert.equal(app.element('step-preview').src, undefined);
});

test('STEP preview errors survive completion warnings for synchronous and delayed failures', () => {
    for (const delay of [0, 50]) {
        const app = panel({ stepError: 'Error creating WebGL context.', stepErrorDelay: delay });
        loadRows(app);
        choose(app, 'C100');
        app.receive('preview', { ...preview(), warnings: ['Symbol layout was regenerated.'],
            preview_3d: { format: 'glb', data_base64: 'Z2xURgIAAAA=' } });
        if (delay) { app.advance(delay); }
        assert.equal(app.element('status').textContent, 'Error creating WebGL context.');
        assert.equal(app.element('status').className, 'error');
        assert.equal(app.element('no-step').hidden, false);
        assert.equal(app.element('place').disabled, false, 'STEP failure does not prevent symbol placement');
    }
});

test('a new selection clears the previous 3D model before requesting another preview', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', { ...preview(), preview_3d: { format: 'glb', data_base64: 'Z2xURgIAAAA=' } });
    assert.equal(app.element('step-preview').hidden, false);
    const clears = app.threeCalls.filter((call) => call.kind === 'clear').length;
    choose(app, 'C200');
    assert.equal(app.element('step-preview').hidden, true);
    assert.equal(app.threeCalls.filter((call) => call.kind === 'clear').length, clears + 1);
    assert.equal(app.element('no-step').hidden, false);
    assert.equal(app.requests.at(-1).data.part, 'C200');
});

test('STEP enlargement reuses the real mesh locally and Escape restores the compact preview', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', { ...preview(), step_status: 'available', step_files: ['part.step'], preview_3d: { format: 'glb', data_base64: 'Z2xURgIAAAA=' } });
    const nativeCalls = app.requests.length;
    app.element('view-step').onclick();
    assert.equal(app.element('zoom').hidden, false);
    assert.equal(app.element('zoom-canvas').hidden, true);
    assert.equal(app.threeCalls.at(-1).kind, 'expand');
    assert.equal(app.threeCalls.at(-1).target, 'zoom');
    assert.equal(app.requests.length, nativeCalls);
    app.keydown('Escape');
    assert.equal(app.element('zoom').hidden, true);
    assert.equal(app.threeCalls.at(-1).kind, 'restore');
    assert.equal(app.element('step-preview').hidden, false);
});

test('changing selection closes an enlarged STEP preview before releasing its model', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', { ...preview(), preview_3d: { format: 'glb', data_base64: 'Z2xURgIAAAA=' } });
    app.element('view-step').onclick();
    choose(app, 'C200');
    assert.equal(app.element('zoom').hidden, true);
    const kinds = app.threeCalls.map((call) => call.kind);
    assert.deepEqual(kinds.slice(-2), ['restore', 'clear']);
});

test('STEP file fallback remains available when no preview mesh exists', () => {
    const app = panel();
    loadRows(app);
    choose(app, 'C100');
    app.receive('preview', { ...preview(), step_status: 'available', step_files: ['part.step'] });
    app.element('view-step').onclick();
    assert.equal(app.requests.at(-1).kind, 'step');
    assert.equal(app.element('zoom').hidden, true);
});
