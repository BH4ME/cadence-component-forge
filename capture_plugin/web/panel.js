/* global OrDzBaseScript, OrDzServerInterface */
(function () {
    'use strict';
    var state = { rows: [], selected: '', page: 1, total: 0, more: false, busy: false, pending: '', query: '', preview: null, serial: 0, pendingId: 0, connected: false, searchCache: Object.create(null) };
    var SEARCH_CACHE_TTL_MS = 5 * 60 * 1000;
    var connectionTimer = null, connectionDeadline = 0, initSent = false;
    var byId = function (id) { return document.getElementById(id); };
    var text = function (id, value) { byId(id).textContent = value == null || value === '' ? '\u2014' : String(value); };
    function status(message, failed) { text('status', message); byId('status').className = failed ? 'error' : ''; }
    function encode(value) { return btoa(unescape(encodeURIComponent(JSON.stringify(value)))); }
    function decode(value) { return JSON.parse(decodeURIComponent(escape(atob(value)))); }
    function bridgeReady() {
        var external = window.external, connector = window.orPrmConnector;
        return !!(window.OrDzServerInterface && external && external.orPrmWidgetId != null &&
            ((connector && typeof connector.setMessageCallback === 'function' && typeof connector.sendMessage === 'function') || typeof external.orPrmConnector === 'function'));
    }
    function connectionFailed(message) {
        clearTimeout(connectionTimer); connectionTimer = null; connectionDeadline = 0;
        state.connected = false; state.pending = ''; busy(false);
        text('connection', 'Capture \u672a\u8fde\u63a5');
        byId('reconnect').hidden = false; byId('reconnect').disabled = false;
        status(message, true);
    }
    function connect() {
        if (state.connected || connectionDeadline) { return; }
        state.connected = false; initSent = false; connectionDeadline = Date.now() + 10000;
        text('connection', '\u6b63\u5728\u8fde\u63a5 Capture...');
        byId('reconnect').hidden = false; byId('reconnect').disabled = true;
        busy(false); status('\u6b63\u5728\u8fde\u63a5 Capture...');
        function poll() {
            connectionTimer = null;
            if (state.connected || !connectionDeadline) { return; }
            if (Date.now() >= connectionDeadline) { connectionFailed('Capture \u8fde\u63a5\u8d85\u65f6\uff0c\u8bf7\u70b9\u51fb\u91cd\u65b0\u8fde\u63a5\u3002'); return; }
            if (!initSent && bridgeReady()) {
                initSent = true;
                if (!request('init')) { return; }
            }
            if (!state.connected && connectionDeadline) { connectionTimer = setTimeout(poll, 100); }
        }
        poll();
    }
    function request(kind, data) {
        if (kind !== 'init' && !state.connected) { return false; }
        if (!bridgeReady()) { connectionFailed('Capture \u8fde\u63a5\u4e0d\u53ef\u7528\uff0c\u8bf7\u91cd\u65b0\u8fde\u63a5\u3002'); return false; }
        data = data || {};
        if (['search', 'preview', 'preview3d', 'import', 'place'].indexOf(kind) !== -1) {
            state.pendingId = ++state.serial;
            data.request_id = state.pendingId;
        }
        var command = '::JLC2CaptureDock::request ' + kind + ' ' + encode(data || {});
        try {
            // A premature isHosted() call permanently selects HTTP; only enter after the native bridge exists.
            OrDzServerInterface.setDocumentMode('local');
            OrDzServerInterface.sGetServerInterface().nativeCall('executeTcl', command, function (response) {
                if (response === 'busy' || response === 'invalid' || response === 'unsupported' || response === 'error') {
                    if (kind === 'init') { connectionFailed('Capture \u521d\u59cb\u5316\u5931\u8d25: ' + response); }
                    else if (response !== 'error') { busy(false); status(response, true); }
                }
            });
        } catch (error) { connectionFailed(error.message); return false; }
        return true;
    }
    function busy(value) {
        state.busy = value;
        byId('progress').hidden = !value;
        var disabled = value || !state.connected;
        var browsingDisabled = !state.connected || (value && !replaceable());
        byId('search').disabled = browsingDisabled;
        byId('folder').disabled = !state.connected;
        byId('updates').disabled = disabled;
        ['manufacturer', 'package', 'in-stock'].forEach(function (id) { byId(id).disabled = browsingDisabled; });
        byId('previous').disabled = browsingDisabled || state.page <= 1;
        byId('next').disabled = browsingDisabled || !state.more;
        byId('import').disabled = disabled || !state.selected;
        byId('place').disabled = disabled || !state.selected;
        byId('view-step').disabled = disabled || !state.preview || !(state.preview.preview_3d || state.preview.step_viewer_url || (state.preview.step_files || []).length);
        byId('load-step').hidden = !state.preview || state.preview.step_status !== 'deferred';
        byId('load-step').disabled = disabled;
    }
    function replaceable() { return ['search', 'preview', 'preview3d'].indexOf(state.pending) !== -1; }
    function search(page, submitted) {
        if ((state.busy && !replaceable()) || !state.connected) { return; }
        var query = submitted || !state.query ? byId('query').value.trim() : state.query;
        if (!query) { return; }
        state.query = query;
        state.pending = 'search';
        state.selected = '';
        clearPreview();
        var cacheKey = query.toLocaleLowerCase() + '\u0000' + page;
        var cached = state.searchCache[cacheKey];
        // An explicit submit is a refresh request; page navigation may use the
        // short-lived in-panel cache for an immediate response.
        if (!submitted && !state.busy && cached && Date.now() - cached.time <= SEARCH_CACHE_TTL_MS) {
            renderSearchResult(cached.data, true);
            return;
        }
        if (cached) { delete state.searchCache[cacheKey]; }
        busy(true);
        status('\u6b63\u5728\u641c\u7d22...');
        if (!request('search', {query: query, page: page})) { busy(false); }
    }
    function options(id, field, label) {
        var select = byId(id), values = {}, previous = select.value;
        state.rows.forEach(function (row) { if (row[field]) { values[row[field]] = true; } });
        select.textContent = '';
        var all = document.createElement('option'); all.value = ''; all.textContent = label; select.appendChild(all);
        Object.keys(values).sort().forEach(function (value) { var option = document.createElement('option'); option.value = value; option.textContent = value; select.appendChild(option); });
        select.value = values[previous] ? previous : '';
    }
    function selectedRow() { return state.rows.filter(function (row) { return row.lcsc_id === state.selected; })[0]; }
    function renderRows() {
        var body = byId('results'), manufacturer = byId('manufacturer').value, pack = byId('package').value, stock = byId('in-stock').checked;
        body.textContent = '';
        var filtered = state.rows.filter(function (row) { return (!manufacturer || row.manufacturer === manufacturer) && (!pack || row.package === pack) && (!stock || (typeof row.stock === 'number' && row.stock > 0)); });
        filtered.forEach(function (row) {
            var tr = document.createElement('tr');
            tr.className = row.lcsc_id === state.selected ? 'selected' : '';
            tr.setAttribute('aria-selected', row.lcsc_id === state.selected ? 'true' : 'false');
            tr.tabIndex = 0;
            [row.lcsc_id, row.title || row.name, row.manufacturer, row.package, row.stock == null ? '\u2014' : row.stock].forEach(function (value) { var td = document.createElement('td'); td.textContent = value == null ? '' : String(value); td.title = td.textContent; tr.appendChild(td); });
            tr.onclick = function () { select(row.lcsc_id); };
            tr.onkeydown = function (event) { if (event.key === 'Enter') { select(row.lcsc_id); } };
            tr.ondblclick = function () { if (!state.busy && state.selected === row.lcsc_id) { importPart('place'); } };
            body.appendChild(tr);
        });
        byId('empty').hidden = filtered.length > 0;
        text('count', filtered.length + ' / ' + state.rows.length + '  \u00b7  ' + state.total);
        text('page', state.page);
        if (state.selected && !filtered.some(function (row) { return row.lcsc_id === state.selected; })) {
            state.selected = ''; clearPreview(); clearDetails();
            if (state.busy && replaceable()) {
                state.pending = ''; state.pendingId = ++state.serial; state.busy = false;
            }
        }
        busy(state.busy);
    }
    function clearDetails() { ['part-title', 'part-id', 'description', 'part-manufacturer', 'part-package', 'step-status'].forEach(function (id) { text(id, ''); }); }
    function clearPreview() { closeZoom(); state.preview = null; if (window.JLC3D) { window.JLC3D.clear(); } byId('no-step').hidden = false; draw('symbol'); draw('footprint'); }
    function renderSearchResult(data, fromCache) {
        if (data.query && data.query !== state.query) { return; }
        state.pending = ''; busy(false);
        state.rows = data.results || []; state.page = data.page || 1; state.total = data.total == null ? state.rows.length : data.total; state.more = !!data.has_more;
        state.selected = ''; clearPreview(); clearDetails(); options('manufacturer', 'manufacturer', '\u5168\u90e8\u5382\u5546'); options('package', 'package', '\u5168\u90e8\u5c01\u88c5'); renderRows();
        var warning = (data.warnings || []).join('\n');
        status(fromCache ? '\u672c\u5730\u7f13\u5b58\uff08\u5feb\u901f\uff09' + (warning ? '\n' + warning : '') : warning);
    }
    function select(part) {
        if ((state.busy && !replaceable()) || !state.connected || (state.busy && state.selected === part)) { return; }
        state.selected = part;
        clearPreview();
        var row = selectedRow();
        text('part-title', row.title || row.name); text('part-id', part); text('description', row.description); text('part-manufacturer', row.manufacturer); text('part-package', row.package); text('step-status', '...');
        renderRows();
        state.pending = 'preview';
        busy(true); status('\u6b63\u5728\u52a0\u8f7d\u539f\u59cb\u7b26\u53f7\u548c\u5c01\u88c5...');
        if (!request('preview', {part: part})) { busy(false); }
    }
    function importPart(kind) {
        if (state.busy || !state.selected) { return; }
        state.pending = kind; busy(true); status('\u6b63\u5728\u751f\u6210 Capture / Allegro \u8d44\u6e90...');
        if (!request(kind, {part: state.selected})) { busy(false); }
    }
    function pointBounds(data) {
        var b = data.bounds;
        if (b && b.length === 4 && b.every(isFinite)) { return b; }
        var points = [];
        (data.graphics || []).forEach(function (g) {
            points = points.concat(g.points || []);
            var p = g.properties || {};
            if (g.kind === 'circle' || g.kind === 'arc') {
                var center = g.kind === 'circle' && g.points && g.points[0];
                var cx = Number(p.center_x_mm == null ? (center ? center[0] : g.x) : p.center_x_mm), cy = Number(p.center_y_mm == null ? (center ? center[1] : g.y) : p.center_y_mm);
                var r = Number(p.radius_mm);
                if (!isFinite(r) && g.points && g.points.length) { r = Math.hypot(g.points[0][0] - cx, g.points[0][1] - cy); }
                if (isFinite(r)) { points.push([cx - r, cy - r], [cx + r, cy + r]); }
            }
            if (g.kind === 'text' && isFinite(g.x) && isFinite(g.y)) {
                points.push([g.x, g.y], [g.x + String(g.text || '').length * 4, g.y + 8]);
            }
        });
        (data.pins || []).forEach(function (p) { points.push([p.x, p.y], [p.end_x, p.end_y]); });
        (data.pads || []).forEach(function (p) {
            var a = (p.rotation || 0) * Math.PI / 180, w = p.size[0] / 2, h = p.size[1] / 2;
            [[-w, -h], [w, -h], [w, h], [-w, h]].forEach(function (q) { points.push([p.x + q[0] * Math.cos(a) - q[1] * Math.sin(a), p.y + q[0] * Math.sin(a) + q[1] * Math.cos(a)]); });
        });
        points = points.filter(function (p) { return isFinite(p[0]) && isFinite(p[1]); });
        if (!points.length) { return [-1, -1, 1, 1]; }
        return points.reduce(function (r, p) { return [Math.min(r[0], p[0]), Math.min(r[1], p[1]), Math.max(r[2], p[0]), Math.max(r[3], p[1])]; }, [Infinity, Infinity, -Infinity, -Infinity]);
    }
    function draw(kind, target) {
        var canvas = target || byId(kind), rect = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.round(rect.width * ratio)); canvas.height = Math.max(1, Math.round(rect.height * ratio));
        var ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, rect.width, rect.height);
        var data = state.preview && state.preview.preview_data && state.preview.preview_data[kind];
        if (!data) { return; }
        var b = pointBounds(data), scale = Math.max(.001, Math.min((rect.width - 28) / Math.max(.01, b[2] - b[0]), (rect.height - 32) / Math.max(.01, b[3] - b[1])));
        var px = function (x) { return rect.width / 2 + (x - (b[0] + b[2]) / 2) * scale; };
        var py = function (y) { return rect.height / 2 - (y - (b[1] + b[3]) / 2) * scale; };
        var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        ctx.lineWidth = 1.3; ctx.strokeStyle = dark ? '#b9d1df' : '#35596b'; ctx.fillStyle = dark ? '#dee8ed' : '#254454';
        (data.graphics || []).forEach(function (graphic) {
            var props = graphic.properties || {}, pts = graphic.points || [];
            if (props.visible === 'false') { return; }
            ctx.strokeStyle = dark ? '#b9d1df' : '#35596b';
            ctx.lineWidth = Math.max(.3, (graphic.width || 0) * scale);
            if (graphic.kind === 'text') {
                var fontSize = Number(props.font_size_mm);
                ctx.save(); ctx.translate(px(graphic.x), py(graphic.y)); ctx.rotate(-(graphic.rotation || 0) * Math.PI / 180);
                ctx.font = (fontSize > 0 ? fontSize * scale : Math.min(10, Math.max(6, 7 * scale))) + 'px ' + (props.font || 'Segoe UI');
                ctx.fillStyle = dark ? '#dee8ed' : '#254454';
                ctx.textAlign = props.anchor === 'end' ? 'right' : props.anchor === 'middle' ? 'center' : 'left'; ctx.textBaseline = props.baseline || 'alphabetic';
                ctx.fillText(graphic.text || '', 0, 0); ctx.restore(); return;
            }
            ctx.beginPath();
            if (graphic.kind === 'circle' || graphic.kind === 'arc') {
                var center = graphic.kind === 'circle' && pts[0];
                var cx = Number(props.center_x_mm == null ? (center ? center[0] : graphic.x) : props.center_x_mm), cy = Number(props.center_y_mm == null ? (center ? center[1] : graphic.y) : props.center_y_mm);
                var radius = Number(props.radius_mm);
                if (!isFinite(radius) && pts.length) { radius = Math.hypot(pts[0][0] - cx, pts[0][1] - cy); }
                if (!(radius > 0)) { return; }
                if (graphic.kind === 'circle') { ctx.arc(px(cx), py(cy), radius * scale, 0, 2 * Math.PI); }
                else {
                    if (pts.length < 2) { return; }
                    ctx.arc(px(cx), py(cy), radius * scale, Math.atan2(cy - pts[0][1], pts[0][0] - cx), Math.atan2(cy - pts[1][1], pts[1][0] - cx), props.clockwise !== 'true');
                }
            } else if (graphic.kind === 'ellipse' && pts.length === 2) {
                ctx.ellipse(px((pts[0][0] + pts[1][0]) / 2), py((pts[0][1] + pts[1][1]) / 2), Math.abs(pts[1][0] - pts[0][0]) * scale / 2, Math.abs(pts[1][1] - pts[0][1]) * scale / 2, 0, 0, 2 * Math.PI);
            } else {
                if (!pts.length) { return; }
                ctx.moveTo(px(pts[0][0]), py(pts[0][1])); pts.slice(1).forEach(function (p) { ctx.lineTo(px(p[0]), py(p[1])); });
                if (graphic.closed || graphic.kind === 'polygon' || graphic.kind === 'rect' || graphic.kind === 'region') { ctx.closePath(); }
            }
            if (props.filled === 'true') { ctx.fillStyle = props.region_type === 'cutout' ? (dark ? '#1d2223' : '#f4f6f7') : ctx.strokeStyle; ctx.fill(); }
            if (graphic.width > 0 || props.filled !== 'true') { ctx.stroke(); }
        });
        (data.pads || []).forEach(function (pad) {
            ctx.save(); ctx.translate(px(pad.x), py(pad.y)); ctx.rotate(-(pad.rotation || 0) * Math.PI / 180);
            var w = pad.size[0] * scale, h = pad.size[1] * scale;
            ctx.fillStyle = '#27a779'; ctx.strokeStyle = '#187453';
            if (pad.shape === 'circle') { ctx.beginPath(); ctx.ellipse(0, 0, w / 2, h / 2, 0, 0, 2 * Math.PI); ctx.fill(); ctx.stroke(); }
            else if (pad.shape === 'oval') {
                var r = Math.min(w, h) / 2;
                ctx.beginPath();
                if (w >= h) { ctx.arc(w / 2 - r, 0, r, -Math.PI / 2, Math.PI / 2); ctx.arc(-w / 2 + r, 0, r, Math.PI / 2, 3 * Math.PI / 2); }
                else { ctx.arc(0, h / 2 - r, r, 0, Math.PI); ctx.arc(0, -h / 2 + r, r, Math.PI, 2 * Math.PI); }
                ctx.closePath(); ctx.fill(); ctx.stroke();
            } else { ctx.fillRect(-w / 2, -h / 2, w, h); ctx.strokeRect(-w / 2, -h / 2, w, h); }
            if (pad.hole_diameter) { ctx.beginPath(); ctx.arc(0, 0, pad.hole_diameter * scale / 2, 0, 2 * Math.PI); ctx.fillStyle = dark ? '#1d2223' : '#f4f6f7'; ctx.fill(); }
            ctx.rotate((pad.rotation || 0) * Math.PI / 180); ctx.font = Math.min(10, Math.max(5, Math.min(w, h) * .65)) + 'px Segoe UI'; ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(pad.number || '', 0, 0); ctx.restore();
        });
        (data.pins || []).forEach(function (pin) {
            var x = px(pin.x), y = py(pin.y), ex = px(pin.end_x), ey = py(pin.end_y);
            ctx.strokeStyle = '#27a779'; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(ex, ey); ctx.stroke();
            if (pin.labels_in_graphics) { return; }
            ctx.fillStyle = dark ? '#dce8ed' : '#254454'; ctx.font = Math.min(12, Math.max(5, 6 * scale)) + 'px Segoe UI'; ctx.textBaseline = 'middle';
            ctx.textAlign = ex >= x ? 'left' : 'right'; ctx.fillText(pin.name || '', ex + (ex >= x ? 3 : -3), ey);
            ctx.textAlign = 'center'; ctx.font = Math.min(10, Math.max(4, 5 * scale)) + 'px Segoe UI'; ctx.fillStyle = '#27a779'; ctx.fillText(pin.number || '', x, y - Math.min(8, 5 * scale));
        });
    }
    window.JLC2Dock = {
        receive: function (kind, encoded, requestId) {
            if (kind === 'paths') {
                try {
                    var registration = decode(encoded);
                    if (registration.ok === false) { status(registration.message || '\u5c01\u88c5\u641c\u7d22\u8def\u5f84\u767b\u8bb0\u5931\u8d25', true); }
                } catch (error) { status(error.message, true); }
                return;
            }
            if (kind !== 'init' && requestId != null && requestId !== state.pendingId) { return; }
            if (kind !== 'init' && kind !== 'error' && kind !== state.pending) { return; }
            var data;
            try { data = decode(encoded); } catch (error) { busy(false); status(error.message, true); return; }
            if (kind === 'init') {
                clearTimeout(connectionTimer); connectionTimer = null; connectionDeadline = 0;
                state.connected = true; text('connection', 'Capture \u5df2\u8fde\u63a5'); byId('reconnect').hidden = true;
                text('library-root', data.library_root); byId('library-root').title = data.library_root; busy(false); status(data.path_warning || '', !!data.path_warning); return;
            }
            if (kind === 'error') {
                state.pending = ''; busy(false);
                var message = [data.user_message, data.message, data.error].filter(function (value) { return typeof value === 'string' && value.trim(); })[0];
                status(message || '\u5bfc\u5165\u5931\u8d25\u3002', true); return;
            }
            if (kind === 'search') {
                if (data.query && data.query !== state.query) { return; }
                state.searchCache[state.query.toLocaleLowerCase() + '\u0000' + (data.page || 1)] = {time: Date.now(), data: data};
                renderSearchResult(data, false);
                return;
            }
            if (data.part_id !== state.selected) { return; }
            state.pending = '';
            state.preview = data; draw('symbol'); draw('footprint');
            text('step-status', {available: '\u53ef\u7528', missing: '\u7f3a\u5931', deferred: '\u6309\u9700\u52a0\u8f7d', mapping_failed: '\u5173\u8054\u5931\u8d25'}[data.step_status] || data.step_status);
            busy(false);
            status((kind === 'preview' || kind === 'preview3d' ? '' : data.footprint_name + '\n') + (data.warnings || []).join('\n'));
            if (data.preview_3d && data.preview_3d.format === 'glb' && window.JLC3D) {
                byId('no-step').hidden = true;
                window.JLC3D.show(data.preview_3d, function (message) { byId('no-step').hidden = false; status(message, true); });
            }
        }
    };
    byId('search-form').onsubmit = function (event) { event.preventDefault(); search(1, true); };
    byId('previous').onclick = function () { search(state.page - 1); };
    byId('next').onclick = function () { search(state.page + 1); };
    ['manufacturer', 'package', 'in-stock'].forEach(function (id) { byId(id).onchange = renderRows; byId(id).title = '\u5f53\u524d\u9875\u7b5b\u9009'; });
    byId('import').onclick = function () { importPart('import'); };
    byId('place').onclick = function () { importPart('place'); };
    byId('folder').onclick = function () { request('folder'); };
    byId('updates').onclick = function () { if (!state.busy) { request('updates'); } };
    byId('reconnect').onclick = connect;
    var zoomKind = '';
    function closeZoom() {
        if (zoomKind === 'step' && window.JLC3D) { window.JLC3D.restore(); }
        byId('zoom').hidden = true; byId('zoom-canvas').hidden = false; zoomKind = '';
    }
    function openZoom(kind) {
        if (!state.preview) { return; }
        if (kind === 'step' && (!state.preview.preview_3d || !window.JLC3D)) { request('step'); return; }
        closeZoom(); zoomKind = kind;
        byId('zoom').hidden = false; byId('zoom-canvas').hidden = kind === 'step';
        text('zoom-title', {symbol: '\u7b26\u53f7', footprint: '\u5c01\u88c5', step: 'STEP'}[kind]);
        if (kind === 'step') {
            if (!window.JLC3D.expand(byId('zoom'))) { closeZoom(); status('\u6b63\u5728\u52a0\u8f7d 3D...'); return; }
        } else { draw(kind, byId('zoom-canvas')); }
        byId('zoom-close').focus();
    }
    byId('view-step').onclick = function () { openZoom('step'); };
    byId('load-step').onclick = function () {
        if (state.busy || !state.selected || !state.preview) { return; }
        state.pending = 'preview3d'; busy(true); status('\u6b63\u5728\u52a0\u8f7d\u539f\u59cb STEP\uff0c\u53ef\u7ee7\u7eed\u9009\u62e9\u5176\u4ed6\u5143\u4ef6...');
        if (!request('preview3d', {part: state.selected})) { busy(false); }
    };
    Array.prototype.forEach.call(document.querySelectorAll('[data-fit]'), function (button) { button.onclick = function () { openZoom(button.getAttribute('data-fit')); }; });
    byId('zoom-close').onclick = closeZoom;
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { closeZoom(); }
        if (event.key === 'Tab' && !byId('zoom').hidden) { event.preventDefault(); byId('zoom-close').focus(); }
    });
    window.addEventListener('resize', function () { draw('symbol'); draw('footprint'); if (zoomKind && zoomKind !== 'step') { draw(zoomKind, byId('zoom-canvas')); } });
    try {
        if (window.lucide) { window.lucide.createIcons({nameAttr: 'data-lucide'}); }
        if (window.OrDzBaseScript) { OrDzBaseScript.sGetBaseScript().setDocumentMode('local'); OrDzBaseScript.sGetBaseScript().setIsSingleJSMode(true, 'OrDzFramework.js'); }
    } catch (error) { status(error.message, true); }
    connect();
}());
