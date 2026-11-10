/* global THREE */
(function () {
    'use strict';
    var root, home, renderer, scene, camera, controls, model, generation = 0, software = false, lastFrame = 0;
    function release(object) {
        object.traverse(function (item) {
            if (item.geometry) { item.geometry.dispose(); }
            var materials = Array.isArray(item.material) ? item.material : item.material ? [item.material] : [];
            materials.forEach(function (material) { Object.keys(material).forEach(function (key) { if (material[key] && material[key].isTexture) { material[key].dispose(); } }); material.dispose(); });
        });
    }
    function dispose() {
        generation += 1;
        if (!model) { return; }
        scene.remove(model); release(model); model = null;
    }
    function size() {
        if (!renderer) { return; }
        var width = Math.max(1, root.clientWidth), height = Math.max(1, root.clientHeight);
        var aspect = width / height;
        if (model && controls) {
            var distanceRatio = Math.min(1, camera.aspect) / Math.min(1, aspect);
            camera.position.sub(controls.target).multiplyScalar(distanceRatio).add(controls.target);
        }
        renderer.setSize(width, height, false); camera.aspect = aspect; camera.updateProjectionMatrix();
    }
    function init() {
        if (renderer) { return; }
        root = document.getElementById('step-preview');
        home = root.parentNode;
        try {
            renderer = new THREE.WebGLRenderer({antialias: true, alpha: false, preserveDrawingBuffer: true});
        } catch (error) {
            renderer = new THREE.SVGRenderer();
            renderer.setPrecision(3);
            software = true;
        }
        root.setAttribute('data-renderer', software ? 'svg' : 'webgl');
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        renderer.outputEncoding = THREE.sRGBEncoding;
        renderer.domElement.setAttribute('aria-label', 'STEP model');
        root.appendChild(renderer.domElement);
        scene = new THREE.Scene();
        scene.background = new THREE.Color(window.matchMedia('(prefers-color-scheme: dark)').matches ? '#202526' : '#f4f6f7');
        camera = new THREE.PerspectiveCamera(35, 1, .001, 10000);
        camera.up.set(0, 0, 1);
        scene.add(new THREE.HemisphereLight(0xffffff, 0x687477, 1.2));
        if (software) { scene.add(new THREE.AmbientLight(0x555555)); }
        var front = new THREE.DirectionalLight(0xffffff, software ? .5 : 1.5); front.position.set(5, 7, 10); scene.add(front);
        var back = new THREE.DirectionalLight(0xffffff, software ? .25 : .8); back.position.set(-7, -3, 5); scene.add(back);
        controls = new THREE.OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true; controls.dampingFactor = .08; controls.enablePan = true;
        controls.autoRotate = true; controls.autoRotateSpeed = .65;
        renderer.domElement.addEventListener('pointerdown', function () { controls.autoRotate = false; });
        window.addEventListener('resize', size);
        if (window.ResizeObserver) { new ResizeObserver(size).observe(root); }
        (function render() {
            requestAnimationFrame(render);
            if (!model || !root.clientWidth || !root.clientHeight) { return; }
            controls.update();
            var now = Date.now();
            if (!software || now - lastFrame >= 1000 / 15) { renderer.render(scene, camera); lastFrame = now; }
        }());
        size();
    }
    window.JLC3D = {
        expand: function (container) {
            if (!model) { return false; }
            container.appendChild(root); size(); return true;
        },
        restore: function () { if (root && home && root.parentNode !== home) { home.appendChild(root); size(); } },
        clear: function () { dispose(); if (root) { root.hidden = true; } },
        show: function (data, failed) {
            try {
                init(); dispose();
                var current = generation;
                var bytes = Uint8Array.from(atob(data.data_base64), function (char) { return char.charCodeAt(0); });
                if (bytes.length < 20 || bytes.length > 6 * 1024 * 1024) { throw new Error('Invalid preview mesh size'); }
                var view = new DataView(bytes.buffer);
                if (view.getUint32(0, true) !== 0x46546c67 || view.getUint32(4, true) !== 2) { throw new Error('Invalid GLB preview'); }
                var jsonLength = view.getUint32(12, true);
                var info = JSON.parse(new TextDecoder().decode(bytes.subarray(20, 20 + jsonLength)));
                (info.buffers || []).concat(info.images || []).forEach(function (asset) { if (asset.uri && asset.uri.indexOf('data:') !== 0) { throw new Error('External mesh assets are not allowed'); } });
                new THREE.GLTFLoader().parse(bytes.buffer, '', function (gltf) {
                    if (current !== generation) { release(gltf.scene); return; }
                    model = gltf.scene;
                    var bounds = new THREE.Box3().setFromObject(model), center = bounds.getCenter(new THREE.Vector3()), dimensions = bounds.getSize(new THREE.Vector3());
                    if (bounds.isEmpty()) { failed('Empty STEP preview geometry'); return; }
                    model.position.sub(center); scene.add(model);
                    root.hidden = false; size();
                    var radius = Math.max(dimensions.x, dimensions.y, dimensions.z), distance = radius * .65 / Math.tan(camera.fov * Math.PI / 360) / Math.min(1, camera.aspect);
                    camera.position.set(distance * .7, distance * .6, distance * .7);
                    camera.near = Math.max(.0001, radius / 1000); camera.far = distance * 20; camera.updateProjectionMatrix();
                    controls.target.set(0, 0, 0); controls.minDistance = radius * .3; controls.maxDistance = distance * 6; controls.autoRotate = true; controls.update();
                    renderer.render(scene, camera);
                }, function (error) { if (current === generation) { failed(error.message || String(error)); } });
            } catch (error) { failed(error.message || String(error)); }
        }
    };
}());
